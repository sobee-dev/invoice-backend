# billing/services.py
"""
Billing service layer.

Two layers live in this file:

1. The provider layer. BillingProvider is the interface, PaystackProvider is the
   only implementation. It knows how to talk to Paystack and nothing about plans.
   It turns Paystack's raw webhook payloads into provider-neutral BillingEvent
   objects. A Flutterwave provider would implement the same interface, and the
   rest of the app would not change.

2. The plan layer. Everything below "Plan logic" works on BillingEvent, Payment and
   Subscription only. Views, jobs and serializers call the public functions in
   that part (create_checkout, confirm_checkout, process_webhook,
   cancel_subscription, get_subscription_manage_link) and never import Paystack
   or requests.

Idempotency: every payment has a unique reference stored in Payment. A payment
moves a period only while Payment.applied_at is empty, and applied_at is set in
the same transaction as the period change. The same reference arriving twice,
from the webhook, the success page, or both at once, extends a period once.
"""
import hashlib
import hmac
import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import emails
from .models import Payment, Subscription
from .plans import (
    BILLING_INTERVALS, BILLING_METHOD_CARD, BILLING_METHOD_ONE_OFF, BUSINESS, CHECKOUT_METHODS,
    INTERVAL_MONTHS, METHOD_CARD, METHOD_TRANSFER, MONTHLY, ONE_OFF_DAYS, PAID_TIERS, PLAN_NAMES,
    _billing_tz, add_months, get_price_ngn, has_live_card_subscription,
)

logger = logging.getLogger(__name__)

PAYSTACK_BASE_URL = 'https://api.paystack.co'

_REFERENCE_PATTERN = re.compile(r'^[A-Za-z0-9_.\-]{6,100}$')
# A renewal charge can land a little before or after the stored period end.
_RENEWAL_SLACK = timedelta(days=3)


# ═════════════════════════════════════════════════════════════════════════════
# Errors and neutral data types
# ═════════════════════════════════════════════════════════════════════════════

class BillingError(Exception):
    """A billing request that failed in a way the owner can be told about."""


class ProviderUnavailable(BillingError):
    """The payment provider could not be reached or returned an HTTP error."""


class InvalidWebhook(BillingError):
    """A webhook whose signature or body could not be trusted."""


# Older code imported PaystackError. It is the same thing now.
PaystackError = BillingError


@dataclass(frozen=True)
class CheckoutSession:
    url: str
    reference: str


@dataclass(frozen=True)
class VerifiedPayment:
    reference: str
    paid: bool
    amount_kobo: int
    currency: str
    channel: str
    metadata: dict


# Event kinds. Providers translate their own event names into these.
PAYMENT_SUCCEEDED = 'payment_succeeded'
SUBSCRIPTION_STARTED = 'subscription_started'
AUTO_RENEW_OFF = 'auto_renew_off'
SUBSCRIPTION_ENDED = 'subscription_ended'
PAYMENT_FAILED = 'payment_failed'


@dataclass(frozen=True)
class BillingEvent:
    kind: str
    reference: str = ''
    business_id: str = ''
    customer_code: str = ''
    subscription_code: str = ''
    auth_token: str = ''
    plan_code: str = ''
    tier: Optional[str] = None
    interval: Optional[str] = None
    amount_kobo: int = 0
    currency: str = 'NGN'
    channel: str = ''
    next_payment_at: Optional[datetime] = None


# ═════════════════════════════════════════════════════════════════════════════
# Provider interface
# ═════════════════════════════════════════════════════════════════════════════

class BillingProvider(ABC):
    """
    What the plan logic needs from a payment provider. The four core methods are
    create_checkout, verify_payment, handle_webhook and cancel. The other three
    are the provider specific lookups the billing page and renewals already
    relied on: plan_code_for, manage_link and next_payment_date.
    """
    name = ''

    @abstractmethod
    def create_checkout(self, business, tier: str, interval: str, method: str, callback_url: str,
                        reference: Optional[str] = None) -> CheckoutSession:
        """Start a payment. method is 'card' (recurring) or 'transfer' (one-off)."""

    @abstractmethod
    def verify_payment(self, reference: str) -> VerifiedPayment:
        """Ask the provider whether this payment really went through."""

    @abstractmethod
    def handle_webhook(self, payload: bytes, signature: str) -> list:
        """Verify the signature, then return the BillingEvents the payload carries.
        Raises InvalidWebhook when the call cannot be trusted."""

    @abstractmethod
    def cancel(self, subscription_code: str, auth_token: str) -> None:
        """Stop a recurring subscription from renewing."""

    @abstractmethod
    def manage_link(self, subscription_code: str) -> str:
        """A provider hosted page where the owner updates their card or cancels."""

    @abstractmethod
    def next_payment_date(self, subscription_code: str) -> Optional[datetime]:
        """Best effort. None when it cannot be found."""

    @abstractmethod
    def plan_code_for(self, tier: str, interval: str) -> str:
        """The provider's recurring plan for this tier and period, or '' if none is set up."""


# ═════════════════════════════════════════════════════════════════════════════
# Paystack
# ═════════════════════════════════════════════════════════════════════════════

def _headers():
    return {'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}', 'Content-Type': 'application/json'}


def _paystack_request(method, path, **kwargs):
    """Paystack returns HTTP 200 with {"status": false, ...} for some
    failures rather than a 4xx, so check the body's status flag, not just
    the HTTP status code, or failures silently look like success."""
    try:
        resp = requests.request(method, f'{PAYSTACK_BASE_URL}{path}', headers=_headers(), timeout=15, **kwargs)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ProviderUnavailable(str(exc)) from exc
    if not data.get('status'):
        raise BillingError(data.get('message', 'Unknown Paystack error'))
    return data['data']


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_when(raw):
    return parse_datetime(raw) if raw else None


class PaystackProvider(BillingProvider):
    name = 'paystack'

    # Plan codes
    # settings.PAYSTACK_PLAN_CODES is {tier: {interval: paystack_plan_code}}.

    def plan_code_for(self, tier, interval) -> str:
        return ((settings.PAYSTACK_PLAN_CODES.get(tier) or {}).get(interval)) or ''

    def _tier_interval_for_code(self, plan_code):
        """(tier, interval) for a Paystack plan code, or (None, None) if it is not one of ours."""
        if plan_code:
            for tier, by_interval in settings.PAYSTACK_PLAN_CODES.items():
                for interval, code in by_interval.items():
                    if code and code == plan_code:
                        return tier, interval
        return None, None

    # Checkout

    def _ensure_customer(self, business) -> str:
        sub, _ = Subscription.objects.get_or_create(business=business)
        if sub.paystack_customer_code:
            return sub.paystack_customer_code

        owner = business.owner
        customer = _paystack_request('POST', '/customer', json={
            'email': owner.email,
            'first_name': owner.first_name or business.name,
            'metadata': {'business_id': str(business.id)},
        })
        sub.paystack_customer_code = customer['customer_code']
        sub.save(update_fields=['paystack_customer_code', 'updated_at'])
        return customer['customer_code']

    def create_checkout(self, business, tier, interval, method, callback_url, reference=None):
        amount_kobo = get_price_ngn(tier, interval) * 100

        if method == METHOD_CARD:
            plan_code = self.plan_code_for(tier, interval)
            if not plan_code:
                raise BillingError('That plan is not available yet.')
            self._ensure_customer(business)
            data = _paystack_request('POST', '/transaction/initialize', json={
                'email': business.owner.email,
                # Paystack uses the plan's own amount when a plan is given. The amount is
                # sent only because the endpoint asks for one, in kobo. A plan also limits
                # the payment to cards, which is why transfer and USSD are a separate call.
                'amount': amount_kobo,
                'plan': plan_code,
                'callback_url': callback_url,
                'metadata': {'business_id': str(business.id), 'tier': tier, 'interval': interval},
            })
            return CheckoutSession(url=data['authorization_url'], reference=data['reference'])

        if method == METHOD_TRANSFER:
            if not reference:
                raise BillingError('A one-off payment needs a reference.')
            data = _paystack_request('POST', '/transaction/initialize', json={
                'email': business.owner.email,
                'amount': amount_kobo,
                'currency': 'NGN',
                'reference': reference,
                'channels': list(getattr(settings, 'BILLING_ONE_OFF_CHANNELS', ['bank_transfer', 'ussd'])),
                'callback_url': callback_url,
                'metadata': {
                    'business_id': str(business.id), 'tier': tier, 'interval': interval,
                    'purpose': 'one_off', 'days': ONE_OFF_DAYS[interval],
                },
            })
            return CheckoutSession(url=data['authorization_url'], reference=data['reference'])

        raise BillingError('Unknown payment method.')

    def verify_payment(self, reference):
        data = _paystack_request('GET', f'/transaction/verify/{reference}')
        return VerifiedPayment(
            reference=data.get('reference') or reference,
            paid=data.get('status') == 'success',
            amount_kobo=int(data.get('amount') or 0),
            currency=data.get('currency') or 'NGN',
            channel=data.get('channel') or '',
            metadata=_as_dict(data.get('metadata')),
        )

    # Subscription management

    def cancel(self, subscription_code, auth_token):
        _paystack_request('POST', '/subscription/disable', json={'code': subscription_code, 'token': auth_token})

    def manage_link(self, subscription_code):
        data = _paystack_request('GET', f'/subscription/{subscription_code}/manage/link')
        return data['link']
    def next_payment_date(self, subscription_code):
        """Best effort. A failed lookup must never fail the webhook."""
        try:
            data = _paystack_request('GET', f'/subscription/{subscription_code}')
        except BillingError:
            logger.warning(f'Could not refresh period end for {subscription_code}', exc_info=True)
            return None
        return _parse_when(data.get('next_payment_date'))

    # Webhooks

    def _signature_ok(self, payload: bytes, signature: str) -> bool:
        """Paystack signs the raw request body with HMAC-SHA512 using your
        secret key. Use compare_digest, not ==, to avoid a timing side-channel."""
        if not signature or not settings.PAYSTACK_SECRET_KEY:
            return False
        computed = hmac.new(settings.PAYSTACK_SECRET_KEY.encode('utf-8'), payload, hashlib.sha512).hexdigest()
        return hmac.compare_digest(computed, signature)

    def handle_webhook(self, payload, signature):
        if not self._signature_ok(payload, signature):
            raise InvalidWebhook('Signature did not match.')
        try:
            event = json.loads(payload)
        except ValueError as exc:
            raise InvalidWebhook('Body is not JSON.') from exc
        if not isinstance(event, dict):
            raise InvalidWebhook('Body is not an object.')

        name = event.get('event')
        data = event.get('data') or {}
        mapper = self._MAPPERS.get(name)
        if mapper is None:
            logger.info(f'Unhandled Paystack event type: {name}')
            return []
        translated = mapper(self, data)
        return [translated] if translated else []

    def _map_charge_success(self, data):
        plan = data.get('plan') or {}
        plan_code = plan.get('plan_code') or ''
        metadata = _as_dict(data.get('metadata'))
        tier, interval = self._tier_interval_for_code(plan_code)
        return BillingEvent(
            kind=PAYMENT_SUCCEEDED,
            reference=data.get('reference') or '',
            business_id=str(metadata.get('business_id') or ''),
            customer_code=(data.get('customer') or {}).get('customer_code') or '',
            plan_code=plan_code,
            tier=tier,
            interval=interval,
            amount_kobo=int(data.get('amount') or 0),
            currency=data.get('currency') or 'NGN',
            channel=data.get('channel') or '',
        )

    def _map_subscription_create(self, data):
        plan = data.get('plan') or {}
        plan_code = plan.get('plan_code') or ''
        tier, mapped_interval = self._tier_interval_for_code(plan_code)
        if plan_code and tier is None:
            logger.error(f'Unknown Paystack plan code {plan_code}, defaulting to business')
        # The interval Paystack reports is what the customer is actually charged, so
        # it wins over our own mapping.
        reported = plan.get('interval')
        interval = reported if reported in BILLING_INTERVALS else mapped_interval
        return BillingEvent(
            kind=SUBSCRIPTION_STARTED,
            business_id=str(_as_dict(data.get('metadata')).get('business_id') or ''),
            customer_code=(data.get('customer') or {}).get('customer_code') or '',
            subscription_code=data.get('subscription_code') or '',
            auth_token=data.get('email_token') or '',
            plan_code=plan_code,
            tier=tier,
            interval=interval,
            next_payment_at=_parse_when(data.get('next_payment_date')),
        )

    def _map_not_renew(self, data):
        return BillingEvent(kind=AUTO_RENEW_OFF, subscription_code=data.get('subscription_code') or '')

    def _map_disable(self, data):
        return BillingEvent(kind=SUBSCRIPTION_ENDED, subscription_code=data.get('subscription_code') or '')

    def _map_payment_failed(self, data):
        code = (data.get('subscription') or {}).get('subscription_code') or ''
        return BillingEvent(kind=PAYMENT_FAILED, subscription_code=code)

    _MAPPERS = {
        'charge.success':         _map_charge_success,
        'subscription.create':    _map_subscription_create,
        'subscription.not_renew': _map_not_renew,
        'subscription.disable':   _map_disable,
        'invoice.payment_failed': _map_payment_failed,
    }


_PROVIDERS = {'paystack': PaystackProvider}


def get_provider() -> BillingProvider:
    name = getattr(settings, 'BILLING_PROVIDER', 'paystack')
    try:
        return _PROVIDERS[name]()
    except KeyError:
        raise BillingError(f'Unknown billing provider: {name}')


# ═════════════════════════════════════════════════════════════════════════════
# Plan logic. Nothing below this line may import or name a specific provider.
# ═════════════════════════════════════════════════════════════════════════════

def ensure_subscription(business) -> Subscription:
    """
    The one way to get a business's subscription row. A missing row is created
    as a trial (the model default sets the 15 day end date), so a trial can
    never exist without an end date. Business creation calls this too.
    """
    sub, _ = Subscription.objects.get_or_create(business=business)
    return sub


def _business_by_id(business_id):
    from business.models import Business
    try:
        return Business.objects.get(id=business_id)
    except (Business.DoesNotExist, ValueError, ValidationError):
        logger.error(f'Billing event referenced unknown business_id={business_id}')
        return None


def _months_for(sub) -> int:
    return INTERVAL_MONTHS.get(sub.billing_interval or MONTHLY, 1)


def _format_date(moment) -> str:
    return moment.astimezone(_billing_tz()).strftime('%d %B %Y').lstrip('0')


# Checkout

def create_checkout(business, tier: str, interval: str, method: str = METHOD_CARD, callback_url: str = '') -> CheckoutSession:
    """
    Starts a payment for one plan and billing period. Returns CheckoutSession:
    the app redirects the owner to url, and sends reference back to the confirm
    endpoint when the provider returns them to the billing page.

    method 'card' is a recurring subscription. method 'transfer' is a one-off
    purchase through bank transfer or USSD that buys ONE_OFF_DAYS[interval] days.
    A one-off gets its own reference and a pending Payment row before the owner
    leaves, so the payment that comes back can be matched and checked.
    """
    if tier not in PAID_TIERS or interval not in BILLING_INTERVALS:
        raise BillingError('Unknown plan.')
    if method not in CHECKOUT_METHODS:
        raise BillingError('Unknown payment method.')

    provider = get_provider()
    if method == METHOD_CARD:
        return provider.create_checkout(business, tier, interval, method, callback_url)

    reference = f'bb_{uuid.uuid4().hex}'
    payment = Payment.objects.create(
        business=business, reference=reference, provider=provider.name, kind=Payment.Kind.ONE_OFF,
        status=Payment.Status.PENDING, tier=tier, interval=interval,
        amount_kobo=get_price_ngn(tier, interval) * 100, currency='NGN',
    )
    try:
        return provider.create_checkout(business, tier, interval, method, callback_url, reference=reference)
    except BillingError:
        payment.delete()
        raise


def confirm_checkout(business, reference: str) -> Subscription:
    """
    Called by the billing page when the provider sends the owner back. Asks the
    provider whether this payment really went through and belongs to this
    business.

    A card payment is marked as waiting for activation, and the webhook does the
    activating. A transfer or USSD payment is applied right here, because nothing
    else is needed to know what was bought. Both routes are safe to repeat and to
    run alongside the webhook, since a payment is applied once per reference.
    """
    if not _REFERENCE_PATTERN.match(reference or ''):
        raise ValueError('Invalid payment reference.')

    provider = get_provider()
    verified = provider.verify_payment(reference)
    if not verified.paid:
        raise BillingError('This payment has not been completed.')

    one_off = Payment.objects.filter(reference=reference, kind=Payment.Kind.ONE_OFF).first()
    if one_off is not None:
        if one_off.business_id != business.id:
            raise BillingError('This payment does not belong to your business.')
        settle_one_off(reference, verified.amount_kobo, verified.currency, verified.channel)
        return ensure_subscription(business)

    metadata = verified.metadata
    if str(metadata.get('business_id')) != str(business.id):
        raise BillingError('This payment does not belong to your business.')

    plan_code = provider.plan_code_for(metadata.get('tier'), metadata.get('interval'))
    sub = ensure_subscription(business)
    already_active = (
        sub.status == Subscription.Status.ACTIVE
        and sub.plan_tier in PAID_TIERS
        and sub.plan_code
        and sub.plan_code == plan_code
        and not sub.cancel_at_period_end
    )
    if already_active:
        return sub

    sub.pending_plan_code = plan_code or None
    sub.pending_checkout_at = timezone.now()
    sub.save(update_fields=['pending_plan_code', 'pending_checkout_at', 'updated_at'])
    return sub


def get_subscription_manage_link(business) -> str:
    """Nearest thing the provider has to a billing portal. Only works for a card
    subscription that already exists (after checkout has completed at least once)."""
    sub = ensure_subscription(business)
    if not sub.paystack_subscription_code or sub.billing_method == BILLING_METHOD_ONE_OFF:
        raise BillingError('No card subscription to manage. Pay again to renew a transfer or USSD plan.')
    return get_provider().manage_link(sub.paystack_subscription_code)


def cancel_subscription(business) -> None:
    sub = ensure_subscription(business)
    if not sub.paystack_subscription_code or not sub.paystack_email_token:
        raise BillingError('No active subscription to cancel.')
    get_provider().cancel(sub.paystack_subscription_code, sub.paystack_email_token)
    # Status flips via the resulting subscription.disable webhook, not here,
    # which keeps one source of truth for state transitions.


def _retire_remote_subscription(code, token) -> None:
    """Best effort. Stops a card subscription we no longer track, so it cannot charge again."""
    if not code or not token:
        return
    try:
        get_provider().cancel(code, token)
    except BillingError:
        logger.error(f'Could not cancel the old card subscription {code}, check it by hand', exc_info=True)


# Webhooks

def process_webhook(payload: bytes, signature: str) -> None:
    """The signature is checked inside the provider on every call. Raises InvalidWebhook."""
    for event in get_provider().handle_webhook(payload, signature):
        apply_event(event)


def apply_event(event: BillingEvent) -> None:
    handler = _EVENT_HANDLERS.get(event.kind)
    if handler is None:
        logger.info(f'Unhandled billing event kind: {event.kind}')
        return
    handler(event)


def _find_subscription(business_id, customer_code):
    if business_id:
        try:
            return Subscription.objects.filter(business_id=business_id).first()
        except (ValueError, ValidationError):
            return None
    if customer_code:
        return Subscription.objects.filter(paystack_customer_code=customer_code).first()
    return None


def _period_end_after_renewal(sub, now):
    """
    Fallback for when the provider cannot be asked. Moves the period end forward by
    one billing period. If the stored end is already well in the future the
    renewal was processed before, so it stays put.
    """
    months = _months_for(sub)
    end = sub.current_period_end
    if end is None:
        return add_months(now, months)
    if end > now + _RENEWAL_SLACK:
        return end
    end = add_months(end, months)
    while end <= now:
        end = add_months(end, months)
    return end


def _roll_past(end, months, now):
    """A stored end that is already behind us, moved forward to the next real end."""
    while end <= now:
        end = add_months(end, months)
    return end


def _sync_customer_code(business_id: str, customer_code: str) -> None:
    business = _business_by_id(business_id)
    if business is None:
        return
    sub = ensure_subscription(business)
    if customer_code and sub.paystack_customer_code != customer_code:
        sub.paystack_customer_code = customer_code
        sub.save(update_fields=['paystack_customer_code', 'updated_at'])


def _handle_payment_succeeded(event: BillingEvent) -> None:
    if not event.reference:
        logger.error('Payment event with no reference, ignored.')
        return

    # A transfer or USSD purchase. We created its Payment row, so trust the row and not the payload.
    if Payment.objects.filter(reference=event.reference, kind=Payment.Kind.ONE_OFF).exists():
        settle_one_off(event.reference, event.amount_kobo, event.currency, event.channel)
        return

    if not event.plan_code:
        return  # a one-off charge unrelated to a subscription plan
    _record_card_charge(event)


def _record_card_charge(event: BillingEvent) -> None:
    """
    A charge on a card subscription. The first charge only records the payment,
    because the subscription.create event that follows it does the activating. A
    renewal brings a failed payment back to active and rolls the period end forward
    by the billing period the customer chose, once per reference.
    """
    if not event.business_id and not event.customer_code:
        logger.error(f'charge.success with no business or customer: {event.reference}')
        return

    if event.business_id:
        _sync_customer_code(event.business_id, event.customer_code)

    sub = _find_subscription(event.business_id, event.customer_code)
    if sub is None:
        return

    # Already counted? Skip before any network call.
    if Payment.objects.filter(reference=event.reference, applied_at__isnull=False).exists():
        logger.info(f'Payment {event.reference} already applied, ignoring the repeat.')
        return

    provider = get_provider()
    renewing = bool(sub.paystack_subscription_code)
    fresh_end = provider.next_payment_date(sub.paystack_subscription_code) if renewing else None

    now = timezone.now()
    with transaction.atomic():
        payment, _ = Payment.objects.select_for_update().get_or_create(
            reference=event.reference,
            defaults=dict(
                business_id=sub.business_id, provider=provider.name, kind=Payment.Kind.CARD,
                status=Payment.Status.SUCCESS, tier=event.tier or '', interval=event.interval or '',
                channel=event.channel, amount_kobo=event.amount_kobo, currency=event.currency,
            ),
        )
        if payment.applied_at is not None:
            return  # another delivery finished first

        locked = Subscription.objects.select_for_update().get(pk=sub.pk)
        if locked.paystack_subscription_code:
            fields = ['updated_at']
            if locked.status == Subscription.Status.PAST_DUE:
                locked.status = Subscription.Status.ACTIVE
                fields.append('status')
            locked.current_period_end = fresh_end or _period_end_after_renewal(locked, now)
            fields.append('current_period_end')
            locked.save(update_fields=fields)
            payment.period_end = locked.current_period_end

        payment.status = Payment.Status.SUCCESS
        payment.applied_at = now
        payment.save(update_fields=['status', 'period_end', 'applied_at', 'updated_at'])


def one_off_period(sub, tier: str, interval: str, now):
    """
    (start, end) for a transfer or USSD purchase. Buying the plan you are already
    on, while it still has time left, adds the days after the current end. Anything
    else (a trial, an expired plan, a different tier) starts today.
    """
    days = ONE_OFF_DAYS[interval]
    carrying = (
        sub.plan_tier == tier
        and sub.status == Subscription.Status.ACTIVE
        and sub.current_period_end is not None
        and sub.current_period_end > now
    )
    start = sub.current_period_end if carrying else now
    return start, start + timedelta(days=days)


def settle_one_off(reference: str, amount_kobo: int, currency: str, channel: str = '') -> bool:
    """
    Applies a confirmed transfer or USSD payment. Returns True only when this call
    moved the period. Safe to call any number of times, from any mix of the
    webhook and the confirm endpoint: the Payment row is locked, and applied_at
    is set in the same transaction as the new period.
    """
    now = timezone.now()
    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(
            reference=reference, kind=Payment.Kind.ONE_OFF,
        ).first()
        if payment is None:
            logger.error(f'One-off payment {reference} has no Payment row, ignored.')
            return False
        if payment.applied_at is not None:
            return False  # already counted

        if int(amount_kobo or 0) != payment.amount_kobo or (currency or 'NGN') != payment.currency:
            logger.error(
                'One-off payment %s amount mismatch: expected %s %s, got %s %s. Not applied.',
                reference, payment.amount_kobo, payment.currency, amount_kobo, currency,
            )
            return False

        business = payment.business
        ensure_subscription(business)
        sub = Subscription.objects.select_for_update().get(business=business)

        if has_live_card_subscription(sub):
            # Checkout refuses this, so it only happens if the owner subscribed by card
            # while this payment was open. Applying it would lose the days at the next renewal.
            payment.status = Payment.Status.NEEDS_REVIEW
            payment.save(update_fields=['status', 'updated_at'])
            logger.error(f'One-off payment {reference} arrived while a card subscription is active. Refund or review by hand.')
            return False

        start, end = one_off_period(sub, payment.tier, payment.interval, now)
        old_code, old_token = sub.paystack_subscription_code, sub.paystack_email_token

        sub.plan_tier = payment.tier
        sub.status = Subscription.Status.ACTIVE
        sub.billing_interval = payment.interval
        sub.billing_method = BILLING_METHOD_ONE_OFF
        sub.current_period_end = end
        # A one-off period never renews. Storing it as "set not to renew" lets the daily
        # job and the effective plan end it on time with no extra rules.
        sub.cancel_at_period_end = True
        sub.pending_plan_code = None
        sub.pending_checkout_at = None
        # Forget any old card subscription so it cannot bill on top of this payment.
        sub.paystack_subscription_code = None
        sub.paystack_email_token = None
        sub.plan_code = None
        sub.save()

        payment.status = Payment.Status.SUCCESS
        payment.channel = channel or payment.channel
        payment.period_start = start
        payment.period_end = end
        payment.applied_at = now
        payment.save(update_fields=['status', 'channel', 'period_start', 'period_end', 'applied_at', 'updated_at'])

        plan_name = PLAN_NAMES[payment.tier]
        ends_on = _format_date(end)
        if old_code:
            transaction.on_commit(lambda: _retire_remote_subscription(old_code, old_token))
        transaction.on_commit(lambda: emails.send_payment_received_email(business, plan_name, ends_on))
    return True


def _handle_subscription_started(event: BillingEvent) -> None:
    if event.business_id:
        business = _business_by_id(event.business_id)
        if business is None:
            return
        sub = ensure_subscription(business)
    else:
        sub = Subscription.objects.filter(paystack_customer_code=event.customer_code).first() if event.customer_code else None
        if sub is None:
            logger.error(f'subscription.create for unknown customer: {event.customer_code}')
            return
    _activate_card_subscription(sub, event)


def _activate_card_subscription(sub: Subscription, event: BillingEvent) -> None:
    """Activates the plan the customer paid for. Records the tier, the billing
    period and the paid-through date, and clears the waiting-for-activation marker.
    Repeating the same event changes nothing."""
    old_code, old_token = sub.paystack_subscription_code, sub.paystack_email_token

    if event.subscription_code:
        sub.paystack_subscription_code = event.subscription_code
    if event.auth_token:
        sub.paystack_email_token = event.auth_token
    if event.plan_code:
        sub.plan_code = event.plan_code
        sub.plan_tier = event.tier or BUSINESS

    sub.billing_interval = event.interval or sub.billing_interval or MONTHLY
    sub.billing_method = BILLING_METHOD_CARD
    sub.status = Subscription.Status.ACTIVE
    # A paid plan always has a real end date, even if the provider omits it.
    sub.current_period_end = event.next_payment_at or add_months(timezone.now(), _months_for(sub))
    sub.cancel_at_period_end = False
    sub.pending_plan_code = None
    sub.pending_checkout_at = None
    sub.save()

    # A new subscription replaced an old one (for example a past due plan paid again).
    if old_code and event.subscription_code and old_code != event.subscription_code:
        transaction.on_commit(lambda: _retire_remote_subscription(old_code, old_token))


def _handle_auto_renew_off(event: BillingEvent) -> None:
    """The owner cancelled but already paid for this period. Keep the plan
    until current_period_end, then the daily job moves them to Free."""
    if not event.subscription_code:
        return
    sub = Subscription.objects.filter(paystack_subscription_code=event.subscription_code).first()
    if sub is None:
        return
    now = timezone.now()
    sub.cancel_at_period_end = True
    fields = ['cancel_at_period_end', 'updated_at']
    # The plan now ends on current_period_end, so that date has to be real. If it
    # is missing or stale (a renewal refresh failed earlier), repair it first, or
    # a yearly customer would be demoted the moment they cancel.
    if sub.current_period_end is None or sub.current_period_end <= now:
        fresh = get_provider().next_payment_date(sub.paystack_subscription_code)
        if fresh and fresh > now:
            sub.current_period_end = fresh
        elif sub.current_period_end is None:
            sub.current_period_end = add_months(now, _months_for(sub))
        else:
            sub.current_period_end = _roll_past(sub.current_period_end, _months_for(sub), now)
        fields.append('current_period_end')
    sub.save(update_fields=fields)


def _handle_subscription_ended(event: BillingEvent) -> None:
    if not event.subscription_code:
        return
    sub = Subscription.objects.filter(paystack_subscription_code=event.subscription_code).first()
    if sub is None or sub.status == Subscription.Status.CANCELED:
        return  # events are redelivered, so only act on the first one

    # Cancelled with paid time left: the customer keeps what they paid for. This
    # is what protects a yearly or six month payer who cancels early.
    now = timezone.now()
    if sub.status == Subscription.Status.ACTIVE and sub.current_period_end and sub.current_period_end > now:
        if not sub.cancel_at_period_end:
            sub.cancel_at_period_end = True
            sub.save(update_fields=['cancel_at_period_end', 'updated_at'])
        return

    sub.status = Subscription.Status.CANCELED
    sub.save(update_fields=['status', 'updated_at'])
    emails.send_subscription_canceled_email(sub.business)


def _handle_payment_failed(event: BillingEvent) -> None:
    if not event.subscription_code:
        return
    sub = Subscription.objects.filter(paystack_subscription_code=event.subscription_code).first()
    if sub is None or sub.status != Subscription.Status.ACTIVE:
        return  # a repeat of a failure we already handled
    # No grace period: the effective plan drops to Free straight away and
    # returns when a charge succeeds.
    sub.status = Subscription.Status.PAST_DUE
    sub.save(update_fields=['status', 'updated_at'])
    emails.send_payment_failed_email(sub.business)


_EVENT_HANDLERS = {
    PAYMENT_SUCCEEDED:    _handle_payment_succeeded,
    SUBSCRIPTION_STARTED: _handle_subscription_started,
    AUTO_RENEW_OFF:       _handle_auto_renew_off,
    SUBSCRIPTION_ENDED:   _handle_subscription_ended,
    PAYMENT_FAILED:       _handle_payment_failed,
}
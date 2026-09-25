# billing/services.py
import hmac
import hashlib
import logging
import requests
from django.conf import settings

from .models import Subscription

logger = logging.getLogger(__name__)
PAYSTACK_BASE_URL = 'https://api.paystack.co'


class PaystackError(Exception):
    pass


def _headers():
    return {'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}', 'Content-Type': 'application/json'}


def _paystack_request(method, path, **kwargs):
    """Paystack returns HTTP 200 with {"status": false, ...} for some
    failures rather than a 4xx — check the body's status flag, not just
    the HTTP status code, or failures silently look like success."""
    resp = requests.request(method, f'{PAYSTACK_BASE_URL}{path}', headers=_headers(), timeout=15, **kwargs)
    resp.raise_for_status()
    data = resp.json()
    if not data.get('status'):
        raise PaystackError(data.get('message', 'Unknown Paystack error'))
    return data['data']


def get_or_create_paystack_customer(business) -> str:
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


def create_subscription_checkout(business, plan_code: str, callback_url: str) -> str:
    """Initializes a transaction bound to a plan. The authorization_url
    is Paystack's equivalent of a Stripe Checkout Session URL — open it
    in a browser/WebView. Completing payment there creates the actual
    subscription; our row updates when the matching webhook lands."""
    get_or_create_paystack_customer(business)
    owner = business.owner

    data = _paystack_request('POST', '/transaction/initialize', json={
        'email': owner.email,
        'plan': plan_code,
        'callback_url': callback_url,
        'metadata': {'business_id': str(business.id)},
    })
    return data['authorization_url']


def get_subscription_manage_link(business) -> str:
    """Nearest thing Paystack has to a billing portal — only works once
    a subscription already exists (i.e. after checkout has completed
    at least once). A business that's never subscribed should be sent
    through create_subscription_checkout instead."""
    sub, _ = Subscription.objects.get_or_create(business=business)
    if not sub.paystack_subscription_code:
        raise PaystackError('No active subscription to manage yet.')
    data = _paystack_request('GET', f'/subscription/manage/link/{sub.paystack_subscription_code}')
    return data['link']


def cancel_subscription(business) -> None:
    sub, _ = Subscription.objects.get_or_create(business=business)
    if not sub.paystack_subscription_code or not sub.paystack_email_token:
        raise PaystackError('No active subscription to cancel.')
    _paystack_request('POST', '/subscription/disable', json={
        'code': sub.paystack_subscription_code,
        'token': sub.paystack_email_token,
    })
    # Status flips to CANCELED via the resulting subscription.disable
    # webhook, not here — keeps one source of truth for state transitions.


# ── Webhook handling ──────────────────────────────────────────────────────

_PLAN_TIER_BY_CODE = {}


def _plan_code_to_tier(plan_code: str) -> str:
    global _PLAN_TIER_BY_CODE
    if not _PLAN_TIER_BY_CODE:
        _PLAN_TIER_BY_CODE = {v: k for k, v in settings.PAYSTACK_PLAN_CODES.items()}
    return _PLAN_TIER_BY_CODE.get(plan_code, Subscription.PlanTier.BASIC)


def verify_webhook_signature(payload: bytes, signature_header: str) -> bool:
    """Paystack signs the raw request body with HMAC-SHA512 using your
    secret key. Use compare_digest, not ==, to avoid a timing side-channel."""
    if not signature_header:
        return False
    computed = hmac.new(settings.PAYSTACK_SECRET_KEY.encode('utf-8'), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature_header)


def handle_paystack_event(event: dict) -> None:
    handler = _HANDLERS.get(event.get('event'))
    if handler is None:
        logger.info(f"Unhandled Paystack event type: {event.get('event')}")
        return
    handler(event['data'])


def _handle_charge_success(data: dict) -> None:
    if not data.get('plan'):
        return  # a one-off charge unrelated to a subscription plan
    business_id = (data.get('metadata') or {}).get('business_id')
    customer_code = (data.get('customer') or {}).get('customer_code')
    if not business_id:
        logger.error(f"charge.success with no business_id metadata: {data.get('reference')}")
        return
    _sync_customer_code(business_id, customer_code)


def _handle_subscription_create(data: dict) -> None:
    business_id = (data.get('metadata') or {}).get('business_id')
    if business_id:
        _sync_subscription(business_id, data)
        return
    customer_code = (data.get('customer') or {}).get('customer_code')
    sub = Subscription.objects.filter(paystack_customer_code=customer_code).first()
    if sub is None:
        logger.error(f"subscription.create for unknown customer: {customer_code}")
        return
    _apply_paystack_subscription(sub, data)


def _handle_subscription_disable(data: dict) -> None:
    sub = Subscription.objects.filter(paystack_subscription_code=data.get('subscription_code')).first()
    if sub is None:
        return
    sub.status = Subscription.Status.CANCELED
    sub.save(update_fields=['status', 'updated_at'])


def _handle_invoice_payment_failed(data: dict) -> None:
    sub_code = (data.get('subscription') or {}).get('subscription_code')
    sub = Subscription.objects.filter(paystack_subscription_code=sub_code).first()
    if sub is None:
        return
    sub.status = Subscription.Status.PAST_DUE
    sub.save(update_fields=['status', 'updated_at'])


def _sync_customer_code(business_id: str, customer_code: str) -> None:
    from business.models import Business
    try:
        business = Business.objects.get(id=business_id)
    except Business.DoesNotExist:
        logger.error(f'Paystack webhook referenced unknown business_id={business_id}')
        return
    sub, _ = Subscription.objects.get_or_create(business=business)
    if customer_code:
        sub.paystack_customer_code = customer_code
        sub.save(update_fields=['paystack_customer_code', 'updated_at'])


def _sync_subscription(business_id: str, data: dict) -> None:
    from business.models import Business
    try:
        business = Business.objects.get(id=business_id)
    except Business.DoesNotExist:
        logger.error(f'Paystack webhook referenced unknown business_id={business_id}')
        return
    sub, _ = Subscription.objects.get_or_create(business=business)
    _apply_paystack_subscription(sub, data)


def _apply_paystack_subscription(sub: Subscription, data: dict) -> None:
    plan = data.get('plan') or {}
    plan_code = plan.get('plan_code')
    sub.paystack_subscription_code = data.get('subscription_code', sub.paystack_subscription_code)
    sub.paystack_email_token = data.get('email_token', sub.paystack_email_token)
    if plan_code:
        sub.plan_code = plan_code
        sub.plan_tier = _plan_code_to_tier(plan_code)
    sub.status = Subscription.Status.ACTIVE
    next_payment = data.get('next_payment_date')
    if next_payment:
        from django.utils.dateparse import parse_datetime
        sub.current_period_end = parse_datetime(next_payment)
    sub.cancel_at_period_end = False
    sub.save()


_HANDLERS = {
    'charge.success':          _handle_charge_success,
    'subscription.create':     _handle_subscription_create,
    'subscription.disable':    _handle_subscription_disable,
    'subscription.not_renew':  _handle_subscription_disable,
    'invoice.payment_failed':  _handle_invoice_payment_failed,
}
# billing/plans.py
"""
Single source of truth for plans.

Everything a business decision touches (limits, prices, billing periods, trial
length, templates, reminder timing) is a constant in the first section. Change
it there and nothing else in the codebase needs to know.

Everything else in the app reads plans through two functions:
  get_effective_plan(business)   which plan applies right now
  check_limit(business, resource) refuse a creation that would pass the cap
Phase 3 extends these two, so no other code should look at Subscription.plan_tier.
"""
import calendar
import math
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone

from django.db import connection
from django.utils import timezone

from .exceptions import PlanLimitReached

# ═════════════════════════════════════════════════════════════════════════════
# EDIT THIS SECTION TO CHANGE BUSINESS RULES
# ═════════════════════════════════════════════════════════════════════════════

TRIAL = 'trial'
FREE = 'free'
BUSINESS = 'business'
ENTERPRISE = 'enterprise'
PAID_TIERS = (BUSINESS, ENTERPRISE)

TRIAL_DAYS = 15

# Countdown emails go out once a day when a trial, or a paid period, has this
# many days or fewer left.
REMINDER_COUNTDOWN_DAYS = 3

# Calendar months for the invoice limit roll over at midnight in this offset.
# Nigeria has no daylight saving, so a fixed offset (UTC+1) is enough.
BILLING_UTC_OFFSET_HOURS = 1

# Billing periods. The names match Paystack's plan intervals exactly, because
# the interval Paystack reports is what the subscription records.
MONTHLY = 'monthly'
BIANNUAL = 'biannually'   # every 6 months
ANNUAL = 'annually'
BILLING_INTERVALS = (MONTHLY, BIANNUAL, ANNUAL)
INTERVAL_MONTHS = {MONTHLY: 1, BIANNUAL: 6, ANNUAL: 12}
INTERVAL_LABELS = {MONTHLY: 'Monthly', BIANNUAL: 'Every 6 months', ANNUAL: 'Yearly'}

# Saving for paying longer, in percent off the monthly price times the months.
INTERVAL_DISCOUNT_PERCENT = {MONTHLY: 0, BIANNUAL: 10, ANNUAL: 20}

# Naira per month on the monthly plan. The amount actually charged on a card is
# whatever the matching Paystack plan says, so create each Paystack plan with the
# amount get_price_ngn(tier, interval) returns (run create_paystack_plans). The
# transfer and USSD purchases are charged get_price_ngn directly.
#

MONTHLY_PRICE_NGN = {BUSINESS: 7500, ENTERPRISE: 20000}

# Card plans renew through a Paystack subscription. Transfer and USSD are one-off
# purchases of a fixed number of days that never renew by themselves. The days
# match the billing periods so every period is offered everywhere.
BILLING_METHOD_CARD = 'card'
BILLING_METHOD_ONE_OFF = 'one_off'
ONE_OFF_DAYS = {MONTHLY: 30, BIANNUAL: 180, ANNUAL: 365}

# Checkout methods the apps can ask for. 'transfer' covers bank transfer and USSD.
METHOD_CARD = 'card'
METHOD_TRANSFER = 'transfer'
CHECKOUT_METHODS = (METHOD_CARD, METHOD_TRANSFER)

# A confirmed payment waits for Paystack's webhook to activate the plan. While
# it waits the plan page shows "confirming" and a second checkout is refused.
# After this many hours the wait is ignored so nobody stays locked out.
PENDING_CHECKOUT_HOURS = 24
# The daily job logs an error for a confirmed payment still waiting after this long.
STUCK_CHECKOUT_MINUTES = 30

# Resource names. These are used as PLAN_LIMITS keys, in the 403 body, and as
# the keys of `limits` and `usage` in the status response.
INVOICES = 'invoices'
PRODUCTS = 'products'
STAFF = 'staff'
BRANCHES = 'branches'
RESOURCES = (INVOICES, PRODUCTS, STAFF, BRANCHES)

# Document types that count toward the monthly invoice limit. Supplier orders
# (purchase invoices) are deliberately left out. Soft-deleted ones still count.
COUNTED_INVOICE_TYPES = ('sales_invoice', 'proforma_invoice')

# Until the Branch model exists in Phase 3 every business has one implicit branch.
BRANCH_COUNT_UNTIL_PHASE_3 = 1

# None means unlimited.
_BUSINESS_LIMITS = {INVOICES: None, PRODUCTS: 300, STAFF: 2, BRANCHES: 1}

PLAN_LIMITS = {
    TRIAL:      _BUSINESS_LIMITS,   # the trial gets Business limits
    FREE:       {INVOICES: 5, PRODUCTS: 10, STAFF: 0, BRANCHES: 1},
    BUSINESS:   _BUSINESS_LIMITS,
    ENTERPRISE: {INVOICES: None, PRODUCTS: None, STAFF: None, BRANCHES: None},
}

ALL_TEMPLATE_IDS = ['classic', 'modern', 'minimal', 'professional']
DEFAULT_TEMPLATE_ID = 'classic'

# Non-numeric plan rules. Add new feature switches here (analytics, ads, and so on).
PLAN_FEATURES = {
    TRIAL:      {'templates': ALL_TEMPLATE_IDS},
    FREE:       {'templates': [DEFAULT_TEMPLATE_ID]},
    BUSINESS:   {'templates': ALL_TEMPLATE_IDS},
    ENTERPRISE: {'templates': ALL_TEMPLATE_IDS},
}

PLAN_NAMES = {TRIAL: 'Trial', FREE: 'Free', BUSINESS: 'Business', ENTERPRISE: 'Enterprise'}

# Wording used in the plan_limit_reached message.
RESOURCE_LABELS = {
    INVOICES: 'invoices per month',
    PRODUCTS: 'products',
    STAFF: 'staff accounts',
    BRANCHES: 'branches',
}

# ═════════════════════════════════════════════════════════════════════════════
# Prices and billing periods
# ═════════════════════════════════════════════════════════════════════════════

def get_price_ngn(tier, interval) -> int:
    """Total naira for one billing period of this plan."""
    months = INTERVAL_MONTHS[interval]
    full = MONTHLY_PRICE_NGN[tier] * months
    return round(full * (100 - INTERVAL_DISCOUNT_PERCENT[interval]) / 100)


def format_ngn(amount) -> str:
    return f"\u20a6{int(amount):,}"


_PERIOD_TEXT = {MONTHLY: 'month', BIANNUAL: '6 months', ANNUAL: 'year'}


def price_label(tier, interval) -> str:
    """Ready-made price text for the apps, for example '₦72,000 / year'."""
    return f"{format_ngn(get_price_ngn(tier, interval))} / {_PERIOD_TEXT[interval]}"


def monthly_equivalent_label(tier, interval) -> str:
    months = INTERVAL_MONTHS[interval]
    return f"{format_ngn(round(get_price_ngn(tier, interval) / months))} / month"


def starting_price_label(tier) -> str:
    """The lowest monthly equivalent across billing periods, for 'from ₦6,000 / month'."""
    best = min(round(get_price_ngn(tier, i) / INTERVAL_MONTHS[i]) for i in BILLING_INTERVALS)
    return f"{format_ngn(best)} / month"


def saving_label(tier, interval):
    """'Save ₦18,000' against paying monthly for the same stretch, or None."""
    saving = MONTHLY_PRICE_NGN[tier] * INTERVAL_MONTHS[interval] - get_price_ngn(tier, interval)
    return f"Save {format_ngn(saving)}" if saving > 0 else None


def discount_label(interval):
    percent = INTERVAL_DISCOUNT_PERCENT[interval]
    return f"Save {percent}%" if percent else None


def add_months(moment, months):
    """moment plus whole calendar months, clamped to the last day of a short month."""
    index = moment.month - 1 + months
    year = moment.year + index // 12
    month = index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def is_checkout_pending(sub, now=None) -> bool:
    """True while a confirmed payment is waiting for the webhook to activate the plan."""
    if sub is None or sub.pending_checkout_at is None:
        return False
    now = now or timezone.now()
    return now - sub.pending_checkout_at < timedelta(hours=PENDING_CHECKOUT_HOURS)


def _limit_text(value) -> str:
    if value is None:
        return 'Unlimited'
    if value == 0:
        return 'Not included'
    return f"{value:,}"


def _templates_text(tier) -> str:
    ids = PLAN_FEATURES[tier]['templates']
    if len(ids) == len(ALL_TEMPLATE_IDS):
        return f"All {len(ids)} templates"
    return ', '.join(i.capitalize() for i in ids) + (' only' if len(ids) == 1 else '')


def build_comparison() -> list:
    """
    Plain Free, Business and Enterprise comparison, built from PLAN_LIMITS and
    PLAN_FEATURES so the billing page can never disagree with what is enforced.
    """
    tiers = (FREE, BUSINESS, ENTERPRISE)
    rows = [
        {
            'key': resource,
            'label': RESOURCE_LABELS[resource].capitalize(),
            'values': {tier: _limit_text(PLAN_LIMITS[tier][resource]) for tier in tiers},
        }
        for resource in RESOURCES
    ]
    rows.append({
        'key': 'templates',
        'label': 'Invoice templates',
        'values': {tier: _templates_text(tier) for tier in tiers},
    })
    return rows


def has_live_card_subscription(sub) -> bool:
    """
    True while a card subscription is active and set to renew. A second purchase
    now would charge the customer twice, so checkout refuses it. A card
    subscription that is cancelled, past due or ended does not count.
    """
    from .models import Subscription

    return bool(
        sub is not None
        and sub.plan_tier in PAID_TIERS
        and sub.status == Subscription.Status.ACTIVE
        and sub.paystack_subscription_code
        and not sub.cancel_at_period_end
        and sub.billing_method != BILLING_METHOD_ONE_OFF
    )


# ═════════════════════════════════════════════════════════════════════════════
# Time helpers
# ═════════════════════════════════════════════════════════════════════════════

def _billing_tz():
    return dt_timezone(timedelta(hours=BILLING_UTC_OFFSET_HOURS))


def invoice_month_bounds(now=None):
    """(start, next_start) of the current calendar month in billing time."""
    local = (now or timezone.now()).astimezone(_billing_tz())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start


def billing_day_start(now=None):
    local = (now or timezone.now()).astimezone(_billing_tz())
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def days_until(end, now=None):
    """Whole days left, rounded up, never below 1 while the date is still ahead."""
    now = now or timezone.now()
    return max(1, math.ceil((end - now).total_seconds() / 86400))


# ═════════════════════════════════════════════════════════════════════════════
# Effective plan
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EffectivePlan:
    tier: str
    limits: dict
    features: dict
    days_left_in_trial: int | None = None

    @property
    def name(self) -> str:
        return PLAN_NAMES[self.tier]

    @property
    def allowed_templates(self) -> list:
        return self.features['templates']


def _resolve_tier(sub, now):
    """
    Returns (tier, days_left_in_trial). Pure function of the stored row and the clock.

    A paid, active subscription is never downgraded because time has passed. The
    billing period (a month, six months or a year) only matters once the owner
    has chosen not to renew. After that the plan ends when the paid period does.
    """
    from .models import Subscription

    if sub is None:
        return FREE, None

    if sub.plan_tier == TRIAL:
        if sub.status == Subscription.Status.TRIALING and sub.trial_ends_at and now < sub.trial_ends_at:
            return TRIAL, days_until(sub.trial_ends_at, now)
        return FREE, None

    if sub.plan_tier in PAID_TIERS and sub.status == Subscription.Status.ACTIVE:
        # A one-off (transfer or USSD) period never renews, so it ends with the paid period.
        no_renewal = sub.cancel_at_period_end or sub.billing_method == BILLING_METHOD_ONE_OFF
        ended = bool(no_renewal and sub.current_period_end and now >= sub.current_period_end)
        return (FREE if ended else sub.plan_tier), None

    # Free, past_due, canceled, or anything unexpected: the floor is Free.
    return FREE, None


def get_effective_plan(business, now=None) -> EffectivePlan:
    """
    The plan that applies to this business right now. Reads the row fresh so a
    caller holding the business lock never sees a stale cached subscription.
    A late daily job never grants extra access, because expiry is decided here
    from the clock, not from what the job has already saved.
    """
    from .models import Subscription

    now = now or timezone.now()
    sub = Subscription.objects.filter(business_id=business.pk).first()
    tier, days_left = _resolve_tier(sub, now)
    return EffectivePlan(
        tier=tier,
        limits=dict(PLAN_LIMITS[tier]),
        features={k: list(v) for k, v in PLAN_FEATURES[tier].items()},
        days_left_in_trial=days_left,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Usage and limits
# ═════════════════════════════════════════════════════════════════════════════

def _count_invoices(business):
    from documents.models import Document
    start, next_start = invoice_month_bounds()
    # all_objects so soft-deleted invoices still count. created_at rather than
    # document_date, because document_date can be backdated.
    return Document.all_objects.filter(
        business=business,
        document_type__in=COUNTED_INVOICE_TYPES,
        created_at__gte=start,
        created_at__lt=next_start,
    ).count()


def _count_products(business):
    from products.models import Product
    return Product.objects.filter(business=business, is_active=True).count()


def _count_staff(business):
    from staff.models import StaffMember
    return StaffMember.objects.filter(business=business, status='active').count()


def _count_branches(business):
    # Phase 3 replaces this with a real count.
    return BRANCH_COUNT_UNTIL_PHASE_3


_COUNTERS = {
    INVOICES: _count_invoices,
    PRODUCTS: _count_products,
    STAFF: _count_staff,
    BRANCHES: _count_branches,
}


def get_usage(business) -> dict:
    return {resource: _COUNTERS[resource](business) for resource in RESOURCES}


def _limit_message(plan, resource, limit):
    label = RESOURCE_LABELS[resource]
    if limit == 0:
        return f"The {plan.name} plan does not include {label}. Upgrade to add them."
    return f"The {plan.name} plan allows {limit} {label}. Upgrade to add more."


def _lock_business(business):
    from business.models import Business
    # The same row DocumentViewSet.perform_create already locks. Taking it first
    # means two simultaneous requests for one business queue up here instead of
    # both counting the same number and both creating.
    list(Business.objects.select_for_update().filter(pk=business.pk).values_list('pk', flat=True))


def check_limit(business, resource):
    """
    Raises PlanLimitReached when creating one more `resource` would pass the
    plan's cap. Must be called inside transaction.atomic, with the creation in
    the same block, because it takes the business row lock that makes the
    count-then-create sequence safe. (SQLite ignores row locks, so the
    concurrency guarantee only holds on Postgres.)
    """
    if resource not in RESOURCES:
        raise ValueError(f'Unknown plan resource: {resource}')
    if not connection.in_atomic_block:
        raise RuntimeError('check_limit must be called inside transaction.atomic.')

    _lock_business(business)
    plan = get_effective_plan(business)
    limit = plan.limits.get(resource)
    if limit is None:
        return

    current = _COUNTERS[resource](business)
    if current >= limit:
        raise PlanLimitReached(
            resource=resource, limit=limit, current=current, plan=plan.tier,
            message=_limit_message(plan, resource, limit),
        )


# ═════════════════════════════════════════════════════════════════════════════
# Staff seats
# ═════════════════════════════════════════════════════════════════════════════

def staff_access_blocked(user) -> bool:
    """
    True when this staff user holds one of the newest seats over the plan's
    staff cap. The oldest `limit` active staff keep working, anyone newer is
    blocked. With a cap of 0 (Free) every staff member is blocked.
    """
    if getattr(user, 'role', None) != 'staff':
        return False

    from staff.models import StaffMember

    seat = (
        StaffMember.objects.filter(user=user, status='active')
        .select_related('business').first()
    )
    if seat is None:
        return False

    limit = get_effective_plan(seat.business).limits.get(STAFF)
    if limit is None:
        return False

    allowed_ids = list(
        StaffMember.objects.filter(business=seat.business, status='active')
        .order_by('created_at', 'id').values_list('id', flat=True)[:limit]
    )
    return seat.id not in allowed_ids
# billing/serializers.py
from rest_framework import serializers

from .models import Subscription
from .plans import (
    INTERVAL_LABELS, TRIAL, get_effective_plan, get_usage, has_live_card_subscription, invoice_month_bounds,
    is_checkout_pending,
)


class SubscriptionStatusSerializer(serializers.Serializer):
    """
    Read-only. Keys are snake_case here and the camel-case renderer turns them
    into plan, status, limits, usage, daysLeftInTrial, billingInterval and so on.
    A null limit means unlimited. Pass context={'business': business} to save a query.

    billing_interval is monthly, biannually or annually (null on trial and Free).
    current_period_end is the date the plan is paid through, which is the renewal
    date, or the end date when cancel_at_period_end is true.
    checkout_pending is true after a confirmed payment while the plan is still
    being activated, so the app can show "confirming your payment" instead of Free.
    billing_method is card (renews by itself), one_off (transfer or USSD, ends on
    current_period_end) or null. can_purchase is false while a card subscription
    is active or a payment is being confirmed, and tells the app whether to offer
    the plan buttons.
    """

    def to_representation(self, sub):
        business = self.context.get('business') or sub.business
        plan = get_effective_plan(business)
        _, resets_at = invoice_month_bounds()

        status = sub.status
        if status == Subscription.Status.TRIALING and plan.tier != TRIAL:
            status = Subscription.Status.ACTIVE  # trial lapsed, the daily job has not saved it yet

        interval = sub.billing_interval or None
        return {
            'plan': plan.tier,
            'plan_name': plan.name,
            'status': status,
            'billing_interval': interval,
            'billing_interval_label': INTERVAL_LABELS.get(interval) if interval else None,
            'billing_method': sub.billing_method or None,
            'limits': plan.limits,
            'usage': get_usage(business),
            'features': plan.features,
            'days_left_in_trial': plan.days_left_in_trial,
            'trial_ends_at': sub.trial_ends_at,
            'current_period_end': sub.current_period_end,
            'cancel_at_period_end': sub.cancel_at_period_end,
            'checkout_pending': is_checkout_pending(sub),
            'can_purchase': not has_live_card_subscription(sub) and not is_checkout_pending(sub),
            'invoices_reset_at': resets_at,
        }
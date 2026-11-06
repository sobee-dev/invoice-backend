# billing/jobs.py
"""
The daily billing job. Plain functions, so it runs the same from a Render Cron
Job (manage.py run_billing_job), from a test, or from a shell.

Access never depends on this job. get_effective_plan decides from the clock, so
a late run cannot extend a trial. The job only saves what is already true and
sends the countdown emails.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import emails
from .models import Subscription
from .plans import (
    BILLING_METHOD_ONE_OFF, DEFAULT_TEMPLATE_ID, PAID_TIERS, PLAN_NAMES, REMINDER_COUNTDOWN_DAYS, STUCK_CHECKOUT_MINUTES, TRIAL,
    billing_day_start, days_until, get_effective_plan,
)

logger = logging.getLogger(__name__)


def expire_trials(now) -> int:
    """Trials past their end date (or somehow without one) become Free."""
    return Subscription.objects.filter(plan_tier=Subscription.PlanTier.TRIAL).filter(
        Q(trial_ends_at__lte=now) | Q(trial_ends_at__isnull=True)
    ).update(
        plan_tier=Subscription.PlanTier.FREE,
        status=Subscription.Status.ACTIVE,
        updated_at=now,
    )


def downgrade_lapsed_paid_plans(now) -> int:
    """
    Paid plans that have already ended become Free: cancelled subscriptions, and
    ones set not to renew whose paid period is over. A past_due plan is left
    alone, since it comes back on its own when a retry succeeds and Paystack
    sends subscription.disable if it never does.
    """
    lapsed = Subscription.objects.filter(plan_tier__in=PAID_TIERS).filter(
        Q(status=Subscription.Status.CANCELED)
        | Q(status=Subscription.Status.ACTIVE, cancel_at_period_end=True, current_period_end__lte=now)
    )
    return lapsed.update(
        plan_tier=Subscription.PlanTier.FREE,
        status=Subscription.Status.ACTIVE,
        billing_interval='',
        billing_method='',
        cancel_at_period_end=False,
        plan_code=None,
        paystack_subscription_code=None,
        paystack_email_token=None,
        updated_at=now,
    )


def reset_locked_templates(now) -> int:
    """A business whose plan no longer includes its chosen template goes back to the default."""
    from business.models import Business, SyncStatus

    reset = 0
    candidates = (
        Business.objects.filter(selected_template_id__isnull=False)
        .exclude(selected_template_id='')
        .exclude(selected_template_id=DEFAULT_TEMPLATE_ID)
    )
    for business in candidates.iterator():
        if business.selected_template_id in get_effective_plan(business, now).allowed_templates:
            continue
        sync_status = SyncStatus.PENDING if business.sync_status == SyncStatus.SYNCED else business.sync_status
        Business.objects.filter(pk=business.pk).update(
            selected_template_id=DEFAULT_TEMPLATE_ID, sync_status=sync_status, updated_at=now,
        )
        reset += 1
    return reset


def report_stuck_checkouts(now) -> int:
    """
    A payment the success page confirmed with Paystack, but whose webhook never
    activated the plan. That customer has paid and is on Free, so it is logged as
    an error. Check the webhook URL and signature key in Paystack, and the
    subscription rows named in the log.
    """
    stuck = Subscription.objects.filter(
        pending_checkout_at__isnull=False,
        pending_checkout_at__lt=now - timedelta(minutes=STUCK_CHECKOUT_MINUTES),
    )
    count = 0
    for sub in stuck:
        count += 1
        logger.error(
            'Paid checkout not activated after %s minutes: business=%s plan_code=%s paid_at=%s',
            STUCK_CHECKOUT_MINUTES, sub.business_id, sub.pending_plan_code, sub.pending_checkout_at,
        )
    return count


def _mark_reminded(sub, now):
    Subscription.objects.filter(pk=sub.pk).update(last_reminder_sent_at=now)


def send_countdown_reminders(now) -> int:
    """
    One email a day, for the last REMINDER_COUNTDOWN_DAYS days before a trial or
    a paid period ends. A user who renews, or who subscribes during the trial,
    drops out of the queries by themselves: a renewal moves current_period_end
    past the window, and a subscribed trial is no longer a trial.
    last_reminder_sent_at keeps it to one per day even if the job runs twice, and
    a failed send is retried on the next run because it is only marked after a
    successful send.
    """
    window_end = now + timedelta(days=REMINDER_COUNTDOWN_DAYS)
    not_yet_today = Q(last_reminder_sent_at__isnull=True) | Q(last_reminder_sent_at__lt=billing_day_start(now))
    sent = 0

    trials = Subscription.objects.filter(
        plan_tier=TRIAL, status=Subscription.Status.TRIALING,
        trial_ends_at__gt=now, trial_ends_at__lte=window_end,
    ).filter(not_yet_today).select_related('business__owner')
    for sub in trials:
        if emails.send_trial_ending_soon_email(sub.business, days_until(sub.trial_ends_at, now)):
            _mark_reminded(sub, now)
            sent += 1

    paid = Subscription.objects.filter(
        plan_tier__in=PAID_TIERS, status=Subscription.Status.ACTIVE,
        current_period_end__gt=now, current_period_end__lte=window_end,
    ).filter(not_yet_today).select_related('business__owner')
    for sub in paid:
        days_left = days_until(sub.current_period_end, now)
        if sub.billing_method == BILLING_METHOD_ONE_OFF:
            # Transfer and USSD plans never renew by themselves, so ask the owner to pay again.
            ok = emails.send_one_off_renewal_reminder_email(sub.business, days_left, PLAN_NAMES[sub.plan_tier])
        elif sub.cancel_at_period_end:
            ok = emails.send_plan_ending_soon_email(sub.business, days_left)
        else:
            ok = emails.send_plan_renewal_soon_email(sub.business, days_left, PLAN_NAMES[sub.plan_tier])
        if ok:
            _mark_reminded(sub, now)
            sent += 1

    return sent


def run_daily_billing_job(now=None) -> dict:
    now = now or timezone.now()
    summary = {
        'trials_expired': expire_trials(now),
        'plans_downgraded': downgrade_lapsed_paid_plans(now),
        'templates_reset': reset_locked_templates(now),
        'stuck_checkouts': report_stuck_checkouts(now),
        'reminders_sent': 0,
    }
    # The flag gates emails only. Expiry and downgrades always run.
    if settings.BILLING_REMINDERS_ENABLED:
        summary['reminders_sent'] = send_countdown_reminders(now)
    logger.info('Daily billing job finished: %s', summary)
    return summary
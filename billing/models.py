# billing/models.py
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

from .plans import BILLING_INTERVALS, BILLING_METHOD_CARD, BILLING_METHOD_ONE_OFF, INTERVAL_LABELS, TRIAL_DAYS

INTERVAL_CHOICES = [(interval, INTERVAL_LABELS[interval]) for interval in BILLING_INTERVALS]


def default_trial_end():
    return timezone.now() + timedelta(days=TRIAL_DAYS)


class Subscription(models.Model):
    class PlanTier(models.TextChoices):
        TRIAL      = 'trial', 'Trial'
        FREE       = 'free', 'Free'
        BUSINESS   = 'business', 'Business'
        ENTERPRISE = 'enterprise', 'Enterprise'

    class Status(models.TextChoices):
        TRIALING = 'trialing', 'Trialing'
        ACTIVE   = 'active', 'Active'      # also used for Free accounts
        PAST_DUE = 'past_due', 'Past Due'  # failed renewal, effective plan is Free
        CANCELED = 'canceled', 'Canceled'

    class BillingMethod(models.TextChoices):
        CARD    = BILLING_METHOD_CARD, 'Card (recurring)'
        ONE_OFF = BILLING_METHOD_ONE_OFF, 'Transfer or USSD (one-off)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField('business.Business', on_delete=models.CASCADE, related_name='subscription')

    plan_tier = models.CharField(max_length=20, choices=PlanTier.choices, default=PlanTier.TRIAL)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.TRIALING)

    # The billing period the customer paid for: monthly, biannually or annually.
    # Empty on trial and Free. Taken from the interval Paystack reports.
    billing_interval = models.CharField(max_length=20, choices=INTERVAL_CHOICES, blank=True, default='')

    # How the current paid period is being paid for. 'card' renews by itself through
    # a Paystack subscription. 'one_off' is a transfer or USSD purchase of a fixed
    # number of days: it never renews, so it is stored with cancel_at_period_end=True
    # and the existing expiry logic ends it on time. Empty on trial and Free.
    billing_method = models.CharField(max_length=20, choices=BillingMethod.choices, blank=True, default='')

    # Paystack identifiers. email_token is required alongside subscription_code
    # for the manage-link and disable calls, Paystack's API insists on both.
    paystack_customer_code     = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    paystack_subscription_code = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    paystack_email_token       = models.CharField(max_length=255, blank=True, null=True)
    plan_code                  = models.CharField(max_length=255, blank=True, null=True)

    # Set when the success page confirms a payment with Paystack, cleared when
    # the webhook activates the plan. Tracks the gap between paying and being active.
    pending_plan_code   = models.CharField(max_length=255, blank=True, null=True)
    pending_checkout_at = models.DateTimeField(null=True, blank=True)

    # Every new row starts a trial, whichever code path creates it, so a trial
    # can never be left without an end date.
    trial_ends_at = models.DateTimeField(null=True, blank=True, default=default_trial_end)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)

    # When the last countdown reminder email went out. The daily job uses it so
    # an owner never gets two reminders on the same day.
    last_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['paystack_customer_code']),
            models.Index(fields=['paystack_subscription_code']),
        ]

    def __str__(self):
        return f"{self.business.name}: {self.plan_tier} ({self.status})"


class Payment(models.Model):
    """
    One row per payment reference. The unique reference is what makes webhooks
    idempotent: a payment extends a period only while applied_at is empty, and
    applied_at is set in the same transaction that moves the period.
    """
    class Kind(models.TextChoices):
        CARD    = 'card', 'Card subscription charge'
        ONE_OFF = 'one_off', 'One-off transfer or USSD purchase'

    class Status(models.TextChoices):
        PENDING      = 'pending', 'Pending'
        SUCCESS      = 'success', 'Success'
        FAILED       = 'failed', 'Failed'
        NEEDS_REVIEW = 'needs_review', 'Needs review'  # paid, but could not be applied safely

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey('business.Business', on_delete=models.CASCADE, related_name='billing_payments')
    reference = models.CharField(max_length=100, unique=True)
    provider = models.CharField(max_length=30, default='paystack')
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    tier = models.CharField(max_length=20, blank=True, default='')
    interval = models.CharField(max_length=20, blank=True, default='')
    channel = models.CharField(max_length=30, blank=True, default='')
    amount_kobo = models.PositiveBigIntegerField(default=0)
    currency = models.CharField(max_length=3, default='NGN')

    # The paid-through dates this payment produced. Empty until it is applied.
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['business', '-created_at'])]

    def __str__(self):
        return f"{self.reference} {self.kind} {self.status}"
# billing/models.py
import uuid
from django.db import models


class Subscription(models.Model):
    class PlanTier(models.TextChoices):
        TRIAL = 'trial', 'Trial'
        BASIC = 'basic', 'Basic'
        PRO   = 'pro', 'Pro'

    class Status(models.TextChoices):
        TRIALING = 'trialing', 'Trialing'
        ACTIVE   = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past Due'
        CANCELED = 'canceled', 'Canceled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField('business.Business', on_delete=models.CASCADE, related_name='subscription')

    plan_tier = models.CharField(max_length=20, choices=PlanTier.choices, default=PlanTier.TRIAL)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.TRIALING)

    # Paystack identifiers. email_token is required alongside subscription_code
    # for the manage-link and disable calls — Paystack's API insists on both.
    paystack_customer_code     = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    paystack_subscription_code = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    paystack_email_token       = models.CharField(max_length=255, blank=True, null=True)
    plan_code                  = models.CharField(max_length=255, blank=True, null=True)

    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    last_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['paystack_customer_code']),
            models.Index(fields=['paystack_subscription_code']),
        ]

    def __str__(self):
        return f"{self.business.name} — {self.plan_tier} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.TRIALING, self.Status.ACTIVE)

    @property
    def is_active_or_grace(self) -> bool:
        from django.utils import timezone
        if self.is_active:
            return True
        if self.status == self.Status.PAST_DUE and self.current_period_end:
            grace_until = self.current_period_end + timezone.timedelta(days=3)
            return timezone.now() < grace_until
        return False

from django.core.management.base import BaseCommand
from django.utils import timezone
from billing.models import Subscription
from billing.emails import send_trial_ending_soon_email, send_grace_period_ending_soon_email
from receipt_backend_api import settings

REMINDER_COOLDOWN = timezone.timedelta(hours=20)


class Command(BaseCommand):
    help = 'Sends trial-ending and grace-period-ending reminder emails. Run daily.'

    def handle(self, *args, **options):

        if not settings.BILLING_REMINDERS_ENABLED:
            self.stdout.write('Billing reminders disabled (BILLING_REMINDERS_ENABLED=False). Skipping.')
            return

        now = timezone.now()

        
        expired_trials = Subscription.objects.filter(
            status=Subscription.Status.TRIALING,
            trial_ends_at__lte=now,
        )
        expired_count = 0
        for sub in expired_trials:
            sub.status = Subscription.Status.PAST_DUE
            sub.current_period_end = sub.trial_ends_at
            sub.save(update_fields=['status', 'current_period_end', 'updated_at'])
            expired_count += 1

        trialing = Subscription.objects.filter(
            status=Subscription.Status.TRIALING,
            trial_ends_at__gt=now,
            trial_ends_at__lte=now + timezone.timedelta(days=2),
        ).select_related('business__owner')

        sent_count = 0
        for sub in trialing:
            if sub.last_reminder_sent_at and now - sub.last_reminder_sent_at < REMINDER_COOLDOWN:
                continue
            send_trial_ending_soon_email(sub.business, max((sub.trial_ends_at - now).days, 0))
            sub.last_reminder_sent_at = now
            sub.save(update_fields=['last_reminder_sent_at'])
            sent_count += 1

        past_due = Subscription.objects.filter(
            status=Subscription.Status.PAST_DUE,
            current_period_end__isnull=False,
        ).select_related('business__owner')

        for sub in past_due:
            hours_left = ((sub.current_period_end + timezone.timedelta(days=3)) - now).total_seconds() / 3600
            if not (0 < hours_left <= 24):
                continue
            if sub.last_reminder_sent_at and now - sub.last_reminder_sent_at < REMINDER_COOLDOWN:
                continue
            send_grace_period_ending_soon_email(sub.business, int(hours_left))
            sub.last_reminder_sent_at = now
            sub.save(update_fields=['last_reminder_sent_at'])
            sent_count += 1

        # Runs last, deliberately after the past_due reminder loop above —
        # anything whose grace window ended today still gets one final
        # "locking soon" email (from the loop, while it still reads
        # PAST_DUE) before being flipped to CANCELED here.
        grace_expired = Subscription.objects.filter(
            status=Subscription.Status.PAST_DUE,
            current_period_end__lte=now - timezone.timedelta(days=3),
        )
        grace_expired_count = grace_expired.update(status=Subscription.Status.CANCELED)

        self.stdout.write(self.style.SUCCESS(
            f'{expired_count} trial(s) expired to grace period, '
            f'{sent_count} reminder(s) sent, '
            f'{grace_expired_count} subscription(s) locked (grace period ended).'
        ))
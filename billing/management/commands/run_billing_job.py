# billing/management/commands/run_billing_job.py
from django.core.management.base import BaseCommand

from billing.jobs import run_daily_billing_job


class Command(BaseCommand):
    help = 'Daily billing job: expire trials, downgrade lapsed plans, send countdown reminders.'

    def handle(self, *args, **options):
        summary = run_daily_billing_job()
        self.stdout.write(self.style.SUCCESS(
            ', '.join(f'{key}={value}' for key, value in summary.items())
        ))
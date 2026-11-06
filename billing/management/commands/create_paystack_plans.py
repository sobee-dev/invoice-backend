# billing/management/commands/create_paystack_plans.py
from django.core.management.base import BaseCommand

from billing.plans import BILLING_INTERVALS, PAID_TIERS, PLAN_NAMES, get_price_ngn
from billing.services import _paystack_request


class Command(BaseCommand):
    help = (
        'Creates the six recurring Paystack plans (Business and Enterprise, each monthly, '
        'every 6 months and yearly) at the prices in billing/plans.py, and prints the '
        'environment variable for each plan code.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Print what would be created and stop.')

    def handle(self, *args, **options):
        for tier in PAID_TIERS:
            for interval in BILLING_INTERVALS:
                amount_kobo = get_price_ngn(tier, interval) * 100
                name = f'BillBuzz {PLAN_NAMES[tier]} {interval}'
                env_name = f'PAYSTACK_PLAN_CODE_{tier.upper()}_{interval.upper()}'
                if options['dry_run']:
                    self.stdout.write(f'{name}: N{amount_kobo // 100:,} -> {env_name}')
                    continue
                data = _paystack_request('POST', '/plan', json={
                    'name': name, 'amount': amount_kobo, 'interval': interval, 'currency': 'NGN',
                })
                self.stdout.write(self.style.SUCCESS(f"{env_name}={data['plan_code']}"))
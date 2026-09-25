# billing/management/commands/create_paystack_plans.py
from django.core.management.base import BaseCommand
from billing.services import _paystack_request


class Command(BaseCommand):
    help = 'Creates the Basic and Pro Paystack plans, prints plan_codes for settings.'

    def handle(self, *args, **options):
        # Amounts are in kobo — ₦9,000/mo = 900000. Adjust to your real pricing.
        plans = [
            {'name': 'BillBuzz Enterprise', 'amount': 700000,  'interval': 'monthly'},
            {'name': 'BillBuzz Corporate',   'amount': 1500000, 'interval': 'monthly'},
        ]
        for plan in plans:
            data = _paystack_request('POST', '/plan', json={**plan, 'currency': 'NGN'})
            self.stdout.write(self.style.SUCCESS(f"{plan['name']}: {data['plan_code']}"))
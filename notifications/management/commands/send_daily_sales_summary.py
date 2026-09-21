from django.core.management.base import BaseCommand

from business.models import Business
from notifications.reports import send_daily_sales_summary_for_business


class Command(BaseCommand):
    help = "Sends the Daily Sales Summary to every business owner. Run once daily, after the business day ends."

    def handle(self, *args, **options):
        sent = 0
        for business in Business.objects.all():
            if not getattr(business, 'owner', None):
                continue
            send_daily_sales_summary_for_business(business)
            sent += 1
        self.stdout.write(self.style.SUCCESS(f"Processed {sent} business(es)."))
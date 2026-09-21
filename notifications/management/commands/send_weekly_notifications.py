from django.core.management.base import BaseCommand

from business.models import Business
from notifications.reports import (
    send_weekly_report_for_business,
    send_best_selling_product_for_business,
    send_fast_moving_stock_for_business,
)


class Command(BaseCommand):
    help = (
        "Sends Weekly Report, Best-Selling Product, and Fast-Moving Stock "
        "notifications to every business owner. Run once weekly."
    )

    def handle(self, *args, **options):
        businesses = [b for b in Business.objects.all() if getattr(b, 'owner', None)]
        for business in businesses:
            send_weekly_report_for_business(business)
            send_best_selling_product_for_business(business)
            send_fast_moving_stock_for_business(business)
        self.stdout.write(self.style.SUCCESS(f"Processed {len(businesses)} business(es)."))
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum, Count, F
from django.utils import timezone

from business.models import Business
from documents.models import Document, DocumentItem
from products.models import Product

from .services import notify

FAST_MOVING_MIN_WEEKLY_UNITS = Decimal('5')
FAST_MOVING_RUNWAY_DAYS = 7

# BillBuzz's primary market is Nigeria, so NGN is the fallback when a
# business record has no currency set. This is a minimal local map —
# if there's already a canonical currency-symbol resolver elsewhere in
# the backend, prefer that over duplicating one here.
_CURRENCY_SYMBOLS = {
    'NGN': '₦', 'USD': '$', 'GBP': '£', 'EUR': '€',
    'GHS': '₵', 'KES': 'KSh', 'ZAR': 'R',
}


def _fmt_money(value: Decimal, currency_code: str) -> str:
    code = (currency_code or 'NGN').upper()
    symbol = _CURRENCY_SYMBOLS.get(code, code + ' ')
    return f"{symbol}{value:,.2f}"


def _fmt_qty(value: Decimal) -> str:
    """Same rendering as products/services.py's _fmt_qty — duplicated
    locally rather than imported, to keep this module's only cross-app
    dependency on products limited to the Product model itself."""
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return str(value.normalize())


def send_daily_sales_summary_for_business(business, target_date=None) -> None:
    """
    Summarizes one business's paid sales invoices for target_date
    (defaults to today) and sends via notify(). Silently skips
    businesses with zero paid transactions that day — an empty
    summary isn't worth a push.

    Uses Document.document_date as the "sales date", matching the
    ordering convention used elsewhere in the app (the mobile
    document list defaults to -document_date). If "date payment was
    recorded" is actually what you want instead of the invoice's
    stated date, this needs a different field.
    """
    target_date = target_date or timezone.localdate()

    qs = Document.objects.filter(
        business=business,
        document_type=Document.DocumentType.SALES_INVOICE,
        status='paid',
        document_date=target_date,
    )
    agg = qs.aggregate(total=Sum('grand_total'), count=Count('id'))
    total = agg['total'] or Decimal('0')
    count = agg['count'] or 0

    if count == 0:
        return

    notify(
        business=business,
        notification_type='daily_sales_summary',
        title="Today's sales",
        body=(
            f"Today's sales: {_fmt_money(total, business.currency)} from {count} "
            f"transaction{'s' if count != 1 else ''}. View your sales report."
        ),
        data={'date': target_date.isoformat(), 'total': str(total), 'count': count},
        dedup_key=target_date.isoformat(),
        cooldown_hours=20,  # safety net against a double cron run, not the real schedule
    )


def send_weekly_report_for_business(business, week_end=None) -> None:
    """
    Trailing 7-day rollup ending on week_end (defaults to today).
    Covers sales and low-stock count — NOT profit (see module-level
    note on why). Skips silently if there's nothing to report: zero
    transactions and zero low-stock products for the week.
    """
    week_end = week_end or timezone.localdate()
    week_start = week_end - timedelta(days=6)

    qs = Document.objects.filter(
        business=business,
        document_type=Document.DocumentType.SALES_INVOICE,
        status='paid',
        document_date__gte=week_start,
        document_date__lte=week_end,
    )
    agg = qs.aggregate(total=Sum('grand_total'), count=Count('id'))
    total = agg['total'] or Decimal('0')
    count = agg['count'] or 0

    low_stock_count = Product.objects.filter(
        business=business,
        is_active=True,
        reorder_level__isnull=False,
        quantity_on_hand__lte=F('quantity_reserved') + F('reorder_level'),
    ).count()

    if count == 0 and low_stock_count == 0:
        return

    notify(
        business=business,
        notification_type='weekly_report',
        title="Your weekly report is ready",
        body="Your weekly report is ready. See your sales and inventory.",
        data={
            'week_start': week_start.isoformat(),
            'week_end': week_end.isoformat(),
            'total_sales': str(total),
            'transaction_count': count,
            'low_stock_count': low_stock_count,
        },
        dedup_key=week_end.isoformat(),
        cooldown_hours=48,
    )


def send_best_selling_product_for_business(business, week_end=None) -> None:
    """
    Trailing 7-day window. Only DocumentItems tied to a real Product
    count — freeform line items with no product match are excluded,
    since there's no product to point the notification at.
    """
    week_end = week_end or timezone.localdate()
    week_start = week_end - timedelta(days=6)

    top = (
        DocumentItem.objects.filter(
            document__business=business,
            document__document_type=Document.DocumentType.SALES_INVOICE,
            document__status='paid',
            document__document_date__gte=week_start,
            document__document_date__lte=week_end,
            product__isnull=False,
        )
        .values('product', 'product__name')
        .annotate(total_qty=Sum('quantity'))
        .order_by('-total_qty')
        .first()
    )

    if top is None or top['total_qty'] <= 0:
        return

    notify(
        business=business,
        notification_type='best_selling_product',
        title="Top seller this week",
        body=(
            f"Top seller: {top['product__name']} is your best-selling product this "
            f"week with {_fmt_qty(top['total_qty'])} units sold."
        ),
        data={
            'product_id': str(top['product']),
            'week_start': week_start.isoformat(),
            'week_end': week_end.isoformat(),
            'units_sold': str(top['total_qty']),
        },
        dedup_key=week_end.isoformat(),
        cooldown_hours=48,
    )


def send_fast_moving_stock_for_business(business, week_end=None) -> None:
    """
    Flags products selling fast enough that current stock runs out
    within FAST_MOVING_RUNWAY_DAYS at this week's pace — a restock-
    timing signal, distinct from Low Stock's static reorder_level
    threshold. A product can trigger this while still above
    reorder_level; that's the point.

    FAST_MOVING_MIN_WEEKLY_UNITS filters out low-volume products where
    a tiny stock count would otherwise look alarming (1 unit sold
    against 1 in stock isn't "fast moving", it's just small numbers).
    Both constants are starting points, not principled values — tune
    them once you see real data.
    """
    week_end = week_end or timezone.localdate()
    week_start = week_end - timedelta(days=6)

    sales_qs = (
        DocumentItem.objects.filter(
            document__business=business,
            document__document_type=Document.DocumentType.SALES_INVOICE,
            document__status='paid',
            document__document_date__gte=week_start,
            document__document_date__lte=week_end,
            product__isnull=False,
        )
        .values('product')
        .annotate(weekly_qty=Sum('quantity'))
        .filter(weekly_qty__gte=FAST_MOVING_MIN_WEEKLY_UNITS)
    )
    sales_by_product = {row['product']: row['weekly_qty'] for row in sales_qs}
    if not sales_by_product:
        return

    products = Product.objects.filter(business=business, id__in=sales_by_product.keys())

    for product in products:
        weekly_qty = sales_by_product[product.id]
        available = product.quantity_on_hand - product.quantity_reserved
        daily_rate = weekly_qty / Decimal('7')
        if daily_rate <= 0:
            continue

        days_left = available / daily_rate
        if days_left > FAST_MOVING_RUNWAY_DAYS:
            continue

        notify(
            business=business,
            notification_type='fast_moving_stock',
            title=f"Fast mover: {product.name}",
            body=(
                f"You sold {_fmt_qty(weekly_qty)} units of {product.name} this week. "
                f"Consider restocking soon."
            ),
            data={
                'product_id': str(product.id),
                'weekly_units_sold': str(weekly_qty),
                'days_of_stock_left': str(days_left.quantize(Decimal('0.1'))),
            },
            dedup_key=f"{product.id}:{week_end.isoformat()}",
            cooldown_hours=48,
        )
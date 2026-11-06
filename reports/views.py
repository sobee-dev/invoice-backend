from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Case, Count, DecimalField, Sum, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from business.models import Business
from documents.models import BALANCE_DUE_EXPR, NET_TOTAL_EXPR, Document

MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

MONEY = DecimalField(max_digits=12, decimal_places=2)
ZERO = Decimal('0.00')


def _month_bounds(year: int, month: int):
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def _shift_month(year: int, month: int, delta: int):
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, (idx % 12) + 1


def _owner_business(request):
    """Returns (business, error_response). Reports are owner only."""
    if request.user.role != 'owner':
        return None, Response(
            {'error': 'Only business owners can access reports.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    business = Business.objects.filter(owner=request.user).first()
    if not business:
        return None, Response({'error': 'No business found.'}, status=status.HTTP_404_NOT_FOUND)
    return business, None


def _unpaid_with_balance(business):
    return (
        Document.objects.filter(
            business=business,
            document_type=Document.DocumentType.SALES_INVOICE,
            status=Document.Status.UNPAID,
        )
        .annotate(outstanding=BALANCE_DUE_EXPR)
        .filter(outstanding__gt=0)
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard(request):
    business, error = _owner_business(request)
    if error:
        return error

    today = timezone.localdate()
    sales_invoices = Document.objects.filter(
        business=business,
        document_type=Document.DocumentType.SALES_INVOICE,
        deleted_at__isnull=True,
    )

    # Last 6 months of paid revenue, net of returned goods, oldest first
    monthly_revenue = []
    for i in range(5, -1, -1):
        y, m = _shift_month(today.year, today.month, -i)
        start, end = _month_bounds(y, m)
        total = sales_invoices.filter(
            status=Document.Status.PAID,
            document_date__gte=start,
            document_date__lt=end,
        ).aggregate(total=Sum(NET_TOTAL_EXPR))['total'] or ZERO
        monthly_revenue.append({
            'month': MONTH_ABBR[m - 1],
            'value': total,
            'current': (y == today.year and m == today.month),
        })

    this_month_start, this_month_end = _month_bounds(today.year, today.month)
    last_year, last_month = _shift_month(today.year, today.month, -1)
    last_month_start, last_month_end = _month_bounds(last_year, last_month)

    this_month_paid = sales_invoices.filter(
        status=Document.Status.PAID,
        document_date__gte=this_month_start,
        document_date__lt=this_month_end,
    )
    paid_agg = this_month_paid.aggregate(total=Sum(NET_TOTAL_EXPR), count=Count('id'))
    this_month_revenue = paid_agg['total'] or ZERO
    this_month_count = paid_agg['count'] or 0

    last_month_revenue = sales_invoices.filter(
        status=Document.Status.PAID,
        document_date__gte=last_month_start,
        document_date__lt=last_month_end,
    ).aggregate(total=Sum(NET_TOTAL_EXPR))['total'] or ZERO

    if last_month_revenue > 0:
        growth_pct = ((this_month_revenue - last_month_revenue) / last_month_revenue) * 100
        revenue_growth = f"{'+' if growth_pct >= 0 else ''}{growth_pct:.1f}%"
    elif this_month_revenue > 0:
        revenue_growth = '+100%'
    else:
        revenue_growth = '—'

    avg_invoice_value = (this_month_revenue / this_month_count) if this_month_count > 0 else ZERO

    # Collection rate: money actually received (including part payments)
    # against what was invoiced this month, net of returns.
    issued = sales_invoices.filter(
        status__in=[Document.Status.PAID, Document.Status.UNPAID],
        document_date__gte=this_month_start,
        document_date__lt=this_month_end,
    ).aggregate(invoiced=Sum(NET_TOTAL_EXPR), collected=Sum('amount_paid'))
    invoiced = issued['invoiced'] or ZERO
    collected = issued['collected'] or ZERO
    collection_rate = f"{min(collected / invoiced, Decimal('1')) * 100:.0f}%" if invoiced > 0 else '—'

    # Outstanding: what is still owed across all customers right now
    outstanding = _unpaid_with_balance(business).aggregate(
        total=Sum('outstanding'), invoices=Count('id'), customers=Count('customer', distinct=True),
    )

    return Response({
        'monthly_revenue':           monthly_revenue,
        'revenue_total':             this_month_revenue,
        'revenue_growth':            revenue_growth,
        'avg_invoice_value':         avg_invoice_value,
        'collection_rate':           collection_rate,
        'outstanding_total':         outstanding['total'] or ZERO,
        'outstanding_invoice_count': outstanding['invoices'] or 0,
        'outstanding_customer_count': outstanding['customers'] or 0,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def outstanding_overview(request):
    """
    GET /api/reports/outstanding/
    Total owed across all customers, ageing buckets by invoice date, and the
    customers who owe the most.
    """
    business, error = _owner_business(request)
    if error:
        return error

    unpaid = _unpaid_with_balance(business)
    today = timezone.localdate()
    c30, c60, c90 = (today - timedelta(days=d) for d in (30, 60, 90))

    ageing = unpaid.aggregate(
        b0=Sum(Case(
            When(document_date__gte=c30, then=BALANCE_DUE_EXPR),
            default=Value(ZERO), output_field=MONEY)),
        b1=Sum(Case(
            When(document_date__lt=c30, document_date__gte=c60, then=BALANCE_DUE_EXPR),
            default=Value(ZERO), output_field=MONEY)),
        b2=Sum(Case(
            When(document_date__lt=c60, document_date__gte=c90, then=BALANCE_DUE_EXPR),
            default=Value(ZERO), output_field=MONEY)),
        b3=Sum(Case(
            When(document_date__lt=c90, then=BALANCE_DUE_EXPR),
            default=Value(ZERO), output_field=MONEY)),
    )
    totals = unpaid.aggregate(total=Sum('outstanding'), invoices=Count('id'), customers=Count('customer', distinct=True))

    top_debtors = list(
        unpaid.exclude(customer__isnull=True)
        .values('customer_id', 'customer__full_name')
        .annotate(total=Sum('outstanding'), invoices=Count('id'))
        .order_by('-total')[:20]
    )

    return Response({
        'total_outstanding': totals['total'] or ZERO,
        'invoice_count': totals['invoices'] or 0,
        'customer_count': totals['customers'] or 0,
        'ageing': [
            {'label': '0 to 30 days', 'amount': ageing['b0'] or ZERO},
            {'label': '31 to 60 days', 'amount': ageing['b1'] or ZERO},
            {'label': '61 to 90 days', 'amount': ageing['b2'] or ZERO},
            {'label': 'Over 90 days', 'amount': ageing['b3'] or ZERO},
        ],
        'top_debtors': [
            {
                'customer_id': row['customer_id'],
                'full_name': row['customer__full_name'],
                'total': row['total'],
                'invoice_count': row['invoices'],
            }
            for row in top_debtors
        ],
    })
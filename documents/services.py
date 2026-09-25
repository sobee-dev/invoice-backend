# documents/services.py
from django.db.models import Sum, Count, Case, When, Value, IntegerField, DecimalField
from django.utils import timezone
from .models import Document
from .serializers import RecentActivitySerializer

def get_invoice_stats(business_id):
    now = timezone.now()
    today = now.date()
    start_of_week = today - timezone.timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)

    invoices = Document.objects.filter(business_id=business_id).select_related('created_by')

    # ── Query 1: revenue (was 3 separate aggregates) ──────────────────
    revenue = invoices.filter(
        document_type=Document.DocumentType.SALES_INVOICE,
        status=Document.Status.PAID,
    ).aggregate(
        total_year=Sum(Case(
            When(document_date__year=now.year, then='grand_total'),
            default=Value(0), output_field=DecimalField(),
        )),
        all_time=Sum('grand_total'),
        monthly=Sum(Case(
            When(document_date__gte=start_of_month, then='grand_total'),
            default=Value(0), output_field=DecimalField(),
        )),
    )

    # ── Query 2: sales + proforma invoice counts (was 8 separate counts) ──
    doc_counts = invoices.aggregate(
        sales_today=Count(Case(When(
            document_type=Document.DocumentType.SALES_INVOICE,
            created_at__date__gte=today, then=1))),
        sales_week=Count(Case(When(
            document_type=Document.DocumentType.SALES_INVOICE,
            created_at__date__gte=start_of_week, then=1))),
        sales_month=Count(Case(When(
            document_type=Document.DocumentType.SALES_INVOICE,
            created_at__date__gte=start_of_month, then=1))),
        sales_all_time=Count(Case(When(
            document_type=Document.DocumentType.SALES_INVOICE, then=1))),
        proforma_today=Count(Case(When(
            document_type=Document.DocumentType.PROFORMA_INVOICE,
            created_at__date__gte=today, then=1))),
        proforma_week=Count(Case(When(
            document_type=Document.DocumentType.PROFORMA_INVOICE,
            created_at__date__gte=start_of_week, then=1))),
        proforma_month=Count(Case(When(
            document_type=Document.DocumentType.PROFORMA_INVOICE,
            created_at__date__gte=start_of_month, then=1))),
        proforma_all_time=Count(Case(When(
            document_type=Document.DocumentType.PROFORMA_INVOICE, then=1))),
        drafts=Count(Case(When(status=Document.Status.DRAFT, then=1))),
    )

    # ── Query 3: recent activity (unchanged — already one query) ──────
    recent_activity = RecentActivitySerializer(
        invoices.order_by('-created_at')[:5], many=True
    ).data

    return {
        "revenue": {
            "total_year": revenue['total_year'] or 0,
            "all_time": revenue['all_time'] or 0,
            "monthly": revenue['monthly'] or 0,
        },
        "sales_invoices": {
            "today": doc_counts['sales_today'],
            "this_week": doc_counts['sales_week'],
            "this_month": doc_counts['sales_month'],
            "all_time": doc_counts['sales_all_time'],
        },
        "proforma_invoices": {
            "today": doc_counts['proforma_today'],
            "this_week": doc_counts['proforma_week'],
            "this_month": doc_counts['proforma_month'],
            "all_time": doc_counts['proforma_all_time'],
        },
        "drafts": doc_counts['drafts'],
        "recent_activity": recent_activity,
    }
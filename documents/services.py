# documents/services.py
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Case, Count, DecimalField, Sum, Value, When
from django.utils import timezone

from customers.models import Customer
from inventory.models import InventoryTransaction
from products.models import Product

from .exceptions import DomainError
from .models import NET_TOTAL_EXPR, Document, DocumentItem
from .serializers import RecentActivitySerializer

THREE_PLACES = Decimal('0.001')
MAX_QUANTITY = Decimal('9999999')
MAX_LINES = 200


# Locking
# Lock order used everywhere in the money and stock code: customer, then
# document, then products (by id). One order means two requests can never
# wait on each other in a circle.
def lock_document(document_id, business):
    """Returns (document, customer_or_None), both row-locked. Call inside
    transaction.atomic."""
    row = Document.objects.filter(pk=document_id, business=business).values('customer_id').first()
    if row is None:
        raise DomainError('Document not found.', 'not_found', 404)

    customer = None
    if row['customer_id']:
        customer = Customer.objects.select_for_update().filter(
            pk=row['customer_id'], business=business,
        ).first()

    try:
        doc = Document.objects.select_for_update().get(pk=document_id, business=business)
    except Document.DoesNotExist:
        raise DomainError('Document not found.', 'not_found', 404)

    if doc.customer_id != row['customer_id']:
        raise DomainError('The document changed while it was being processed. Try again.', 'conflict', 409)
    return doc, customer


def parse_quantity(value, label='Quantity'):
    try:
        qty = Decimal(str(value))
        if not qty.is_finite():
            raise InvalidOperation
        rounded = qty.quantize(THREE_PLACES)
    except (InvalidOperation, ValueError, TypeError):
        raise DomainError(f'{label} must be a valid number.', 'invalid_quantity')
    if rounded != qty:
        raise DomainError(f'{label} can have at most 3 decimal places.', 'invalid_quantity')
    if qty <= 0 or qty > MAX_QUANTITY:
        raise DomainError(f'{label} must be greater than zero.', 'invalid_quantity')
    return qty


# Stock deduction
def apply_deduction_locked(doc, user, items_payload=None, subtract_all=False, reasons=None):
    """Deducts stock for a sales invoice. The caller must hold the document lock
    (see lock_document) inside an atomic block."""
    if doc.document_type != Document.DocumentType.SALES_INVOICE:
        raise DomainError('Stock deduction is only valid for sales invoices.', 'invalid_type')
    if doc.status == Document.Status.DELETED:
        raise DomainError('This document has been deleted.', 'invalid_status', 409)
    if doc.stock_status == Document.StockStatus.DEDUCTED:
        raise DomainError('Stock has already been deducted for this document.', 'already_deducted', 409)

    tracked = list(
        DocumentItem.objects.filter(document=doc, product__isnull=False).order_by('order', 'id')
    )
    if not tracked:
        raise DomainError('This document has no inventory-tracked items.', 'no_tracked_items')
    by_id = {str(i.id): i for i in tracked}

    if subtract_all:
        targets = [(i, i.quantity - i.quantity_deducted) for i in tracked if i.quantity > i.quantity_deducted]
    else:
        if not isinstance(items_payload, list) or not items_payload:
            raise DomainError('Provide subtract_all=true or a non-empty items list.', 'invalid_payload')
        if len(items_payload) > MAX_LINES:
            raise DomainError('Too many items in one request.', 'invalid_payload')
        seen = set()
        targets = []
        for entry in items_payload:
            if not isinstance(entry, dict):
                raise DomainError('Each item must be an object.', 'invalid_payload')
            try:
                key = str(uuid.UUID(str(entry.get('item_id', ''))))
            except ValueError:
                raise DomainError('Invalid item id.', 'invalid_payload')
            if key in seen:
                raise DomainError('The same item appears more than once.', 'duplicate_item')
            seen.add(key)
            item = by_id.get(key)
            if item is None:
                raise DomainError('Item not found on this document.', 'item_not_found', 404)
            qty = parse_quantity(entry.get('quantity'))
            remaining = item.quantity - item.quantity_deducted
            if qty > remaining:
                raise DomainError(
                    f"'{item.description}': only {remaining} left to deduct.", 'exceeds_remaining',
                )
            targets.append((item, qty))

    if not targets:
        raise DomainError('Nothing is left to deduct.', 'nothing_to_deduct', 409)

    # Sum per product first so two lines for the same product cannot overdraw.
    needed = defaultdict(Decimal)
    for item, qty in targets:
        needed[item.product_id] += qty

    products = {
        p.id: p
        for p in Product.objects.select_for_update()
        .filter(id__in=list(needed), business_id=doc.business_id)
        .order_by('id')
    }
    if set(needed) - set(products):
        raise DomainError('A product on this invoice is no longer available.', 'product_missing', 409)

    for pid, qty in needed.items():
        product = products[pid]
        if product.quantity_on_hand < qty:
            raise DomainError(
                f"Insufficient stock for '{product.name}'. "
                f"Available: {product.quantity_on_hand}, requested: {qty}.",
                'insufficient_stock',
            )

    for item, qty in targets:
        InventoryTransaction.objects.create(
            business_id=doc.business_id,
            product=products[item.product_id],
            quantity_change=-qty,
            transaction_type=InventoryTransaction.TransactionType.SALES_CONFIRMED,
            reference_document_id=doc.id,
            initiated_by=user,
            reason=(
                (reasons or {}).get(str(item.id)) or f'Sale {doc.document_number}'
            )[:255],
        )
        item.quantity_deducted += qty
        item.save(update_fields=['quantity_deducted'])

    for pid, qty in needed.items():
        product = products[pid]
        product.quantity_on_hand -= qty
        product.save(update_fields=['quantity_on_hand', 'updated_at'])

    doc.refresh_stock_status()
    return doc


def deduct_available_locked(doc, user):
    """
    Used when a draft is being marked unpaid. Deducts as much of each item as
    stock allows (possibly less than the invoiced quantity), skips items with
    no stock at all, records the shortfall in the stock history, then closes
    the one-time deduction. Caller must hold the document lock inside an
    atomic block.
    """
    if doc.document_type != Document.DocumentType.SALES_INVOICE:
        raise DomainError('Stock deduction is only valid for sales invoices.', 'invalid_type')
    if doc.stock_deducted_at is not None:
        return doc

    items = list(
        DocumentItem.objects.filter(document=doc, product__isnull=False).order_by('order', 'id')
    )
    products = {
        p.id: p
        for p in Product.objects.select_for_update()
        .filter(id__in={i.product_id for i in items}, business_id=doc.business_id)
        .order_by('id')
    }
    available = {pid: p.quantity_on_hand for pid, p in products.items()}

    lines = []
    reasons = {}
    for item in items:
        remaining = item.quantity - item.quantity_deducted
        if remaining <= 0:
            continue
        take = min(remaining, available.get(item.product_id, Decimal('0')))
        if take <= 0:
            continue  # nothing in stock, nothing to record
        available[item.product_id] -= take  # same product on two lines

        lines.append({'item_id': str(item.id), 'quantity': take})
        if take < remaining:
            reasons[str(item.id)] = (
                f'Sale {doc.document_number}: only {take} of {remaining} deducted (insufficient stock)'
            )

    if lines:
        apply_deduction_locked(doc, user, lines, False, reasons)
    _close_deduction(doc)
    return doc

def _assert_deduction_open(doc):
    """Manual deduction rules: never on drafts, and only once per invoice."""
    if doc.status == Document.Status.DRAFT:
        raise DomainError(
            'Stock can only be deducted once the invoice is unpaid or paid.', 'draft_not_allowed', 409,
        )
    if doc.stock_deducted_at is not None:
        raise DomainError(
            'Stock has already been deducted for this invoice and cannot be deducted again.',
            'deduction_closed', 409,
        )


def _close_deduction(doc):
    """Marks the one-time deduction as used, even if nothing could be deducted."""
    if doc.stock_deducted_at is None:
        doc.stock_deducted_at = timezone.now()
        doc.save(update_fields=['stock_deducted_at', 'updated_at'])


@transaction.atomic
def deduct_stock(*, document_id, business, user, items=None, subtract_all=False):
    doc, _customer = lock_document(document_id, business)
    _assert_deduction_open(doc)
    apply_deduction_locked(doc, user, items, subtract_all)
    _close_deduction(doc)
    return doc

# Dashboard statistics
def get_invoice_stats(business_id):
    now = timezone.now()
    today = now.date()
    start_of_week = today - timezone.timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)

    invoices = Document.objects.filter(business_id=business_id).select_related('created_by')

    # Revenue is net of returned goods.
    revenue = invoices.filter(
        document_type=Document.DocumentType.SALES_INVOICE,
        status=Document.Status.PAID,
    ).aggregate(
        total_year=Sum(Case(
            When(document_date__year=now.year, then=NET_TOTAL_EXPR),
            default=Value(0), output_field=DecimalField(),
        )),
        all_time=Sum(NET_TOTAL_EXPR),
        monthly=Sum(Case(
            When(document_date__gte=start_of_month, then=NET_TOTAL_EXPR),
            default=Value(0), output_field=DecimalField(),
        )),
    )

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
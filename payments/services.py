"""
All money and stock-return logic lives here. Rules every function follows:

1. Runs inside transaction.atomic, so a failure leaves nothing half done.
2. Locks rows in one fixed order: customer, then document(s), then products.
3. Decides from locked rows, never from data read before the lock.
4. Raises DomainError for rule violations, which rolls everything back.
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from customers.models import Customer
from documents.exceptions import DomainError
from documents.models import BALANCE_DUE_EXPR, Document, DocumentItem
from documents.services import apply_deduction_locked, deduct_available_locked, lock_document
from inventory.models import InventoryTransaction
from products.models import Product

from .models import ItemReturn, Payment, PaymentAllocation

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# Shared helpers
def calculate_outstanding(customer):
    """Source of truth: the sum of balances on the customer's unpaid sales invoices."""
    total = Document.objects.filter(
        business_id=customer.business_id,
        customer=customer,
        document_type=Document.DocumentType.SALES_INVOICE,
        status=Document.Status.UNPAID,
    ).aggregate(total=Sum(BALANCE_DUE_EXPR))['total'] or ZERO
    return max(money(total), ZERO)


def recalculate_outstanding(customer):
    customer.outstanding_balance = calculate_outstanding(customer)
    customer.save(update_fields=['outstanding_balance'])


def _require_sales(doc):
    if doc.document_type != Document.DocumentType.SALES_INVOICE:
        raise DomainError('This action is only valid for sales invoices.', 'invalid_type')


def _require_stock_settled(doc):
    if not doc.is_stock_settled:
        raise DomainError(
            'Deduct all items from stock before working on this invoice.', 'stock_not_deducted', 409,
        )


def _lock_products(product_ids, business_id):
    products = {
        p.id: p
        for p in Product.objects.select_for_update()
        .filter(id__in=list(product_ids), business_id=business_id)
        .order_by('id')
    }
    if set(product_ids) - set(products):
        raise DomainError('A product on this invoice is no longer available.', 'product_missing', 409)
    return products


# Mark unpaid
@transaction.atomic
def mark_invoice_unpaid(*, document_id, business, user, deduct_stock=False):
    doc, customer = lock_document(document_id, business)
    _require_sales(doc)
    if doc.status != Document.Status.DRAFT:
        raise DomainError('Only draft sales invoices can be marked unpaid.', 'invalid_status', 409)
    if customer is None:
        raise DomainError('Attach a customer to this invoice before marking it unpaid.', 'customer_required')
    if doc.grand_total <= 0:
        raise DomainError('An invoice with no value cannot be unpaid.', 'invalid_total')

    if doc.stock_deducted_at is None and doc.stock_status == Document.StockStatus.PENDING:
        if not deduct_stock:
            raise DomainError(
                'Stock must be deducted when marking this invoice unpaid.', 'stock_not_deducted', 409,
            )
        deduct_available_locked(doc, user)

    doc.status = Document.Status.UNPAID
    doc.save(update_fields=['status', 'updated_at'])
    recalculate_outstanding(customer)
    return doc


# Payments
def _apply_payment(*, business, customer, targets, amount, method, reference, note, user, idempotency_key=None):
    """Caller holds the customer and document locks."""
    if not targets:
        raise DomainError('There are no unpaid invoices to apply this payment to.', 'nothing_outstanding', 409)
    for d in targets:
        _require_stock_settled(d)

    total_due = sum((d.balance_due for d in targets), ZERO)
    if amount > total_due:
        raise DomainError(
            f'Payment of {amount} is more than the outstanding balance of {total_due}.', 'overpayment',
        )

    payment = Payment.objects.create(
        business=business, customer=customer, amount=amount, method=method,
        reference=reference, note=note, idempotency_key=idempotency_key, recorded_by=user,
    )

    remaining = amount
    now = timezone.now()
    for d in targets:
        if remaining <= 0:
            break
        balance = d.balance_due
        if balance <= 0:
            continue
        applied = min(balance, remaining)
        PaymentAllocation.objects.create(payment=payment, document=d, amount=applied)
        d.amount_paid += applied
        fields = ['amount_paid', 'updated_at']
        if d.balance_due <= 0:
            d.status = Document.Status.PAID
            d.paid_at = now
            fields += ['status', 'paid_at']
        d.save(update_fields=fields)
        remaining -= applied

    recalculate_outstanding(customer)
    return payment


@transaction.atomic
def record_payment(*, business, customer_id, user, amount, method, reference='', note='',
                   document_id=None, idempotency_key=None):
    """Returns (payment, created). With document_id the payment goes to that
    invoice only; without it, it is applied oldest invoice first."""
    customer = Customer.objects.select_for_update().filter(pk=customer_id, business=business).first()
    if customer is None:
        raise DomainError('Customer not found.', 'not_found', 404)

    if idempotency_key:
        existing = Payment.objects.filter(business=business, idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.customer_id != customer.id or existing.amount != amount:
                raise DomainError('That request key was already used for a different payment.',
                                  'idempotency_conflict', 409)
            return existing, False

    qs = Document.objects.select_for_update().filter(
        business=business, customer=customer,
        document_type=Document.DocumentType.SALES_INVOICE, status=Document.Status.UNPAID,
    )
    if document_id:
        qs = qs.filter(pk=document_id)
    targets = list(qs.order_by('document_date', 'created_at', 'id'))

    if document_id and not targets:
        raise DomainError('Unpaid invoice not found for this customer.', 'not_found', 404)
    if not document_id:
        targets = [d for d in targets if d.is_stock_settled and d.balance_due > 0]

    payment = _apply_payment(
        business=business, customer=customer, targets=targets, amount=amount, method=method,
        reference=reference, note=note, user=user, idempotency_key=idempotency_key,
    )
    return payment, True


@transaction.atomic
def mark_invoice_paid(*, document_id, business, user, method='cash', reference=''):
    doc, customer = lock_document(document_id, business)
    if doc.document_type == Document.DocumentType.PURCHASE_INVOICE:
        raise DomainError('Supplier orders cannot be marked paid.', 'invalid_type')
    if doc.status == Document.Status.PAID:
        raise DomainError('This document is already paid.', 'already_paid', 409)
    if doc.status == Document.Status.DELETED:
        raise DomainError('This document has been deleted.', 'invalid_status', 409)

    now = timezone.now()
    if doc.document_type == Document.DocumentType.SALES_INVOICE and doc.status == Document.Status.UNPAID:
        if customer is None:
            raise DomainError('This invoice has no customer attached.', 'customer_required')
        if doc.balance_due > 0:
            _apply_payment(
                business=business, customer=customer, targets=[doc], amount=money(doc.balance_due),
                method=method, reference=reference, note='Marked as paid', user=user,
            )
        else:
            doc.status = Document.Status.PAID
            doc.paid_at = now
            doc.save(update_fields=['status', 'paid_at', 'updated_at'])
            recalculate_outstanding(customer)
        return doc

    # Draft sale or proforma: settled in one step, no credit ledger involved.
    doc.amount_paid = max(doc.grand_total - doc.returned_credit, ZERO)
    doc.status = Document.Status.PAID
    doc.paid_at = now
    doc.save(update_fields=['amount_paid', 'status', 'paid_at', 'updated_at'])
    if customer is not None:
        recalculate_outstanding(customer)
    return doc


@transaction.atomic
def void_payment(*, payment_id, business, user, reason):
    row = Payment.objects.filter(pk=payment_id, business=business).values('customer_id').first()
    if row is None:
        raise DomainError('Payment not found.', 'not_found', 404)

    customer = Customer.objects.select_for_update().get(pk=row['customer_id'], business=business)
    payment = Payment.objects.select_for_update().get(pk=payment_id, business=business)
    if payment.voided_at is not None:
        raise DomainError('This payment has already been voided.', 'already_voided', 409)

    allocations = list(PaymentAllocation.objects.filter(payment=payment))
    docs = {
        d.id: d
        for d in Document.objects.select_for_update()
        .filter(pk__in=[a.document_id for a in allocations]).order_by('id')
    }
    for a in allocations:
        d = docs.get(a.document_id)
        if d is None:
            raise DomainError('A linked invoice is no longer available.', 'conflict', 409)
        d.amount_paid -= a.amount
        if d.amount_paid < 0:
            raise DomainError('Payment records are inconsistent for this invoice.', 'inconsistent', 409)
        fields = ['amount_paid', 'updated_at']
        if d.status == Document.Status.PAID and d.balance_due > 0:
            d.status = Document.Status.UNPAID
            d.paid_at = None
            fields += ['status', 'paid_at']
        d.save(update_fields=fields)

    payment.voided_at = timezone.now()
    payment.voided_by = user
    payment.void_reason = reason
    payment.save(update_fields=['voided_at', 'voided_by', 'void_reason'])
    recalculate_outstanding(customer)
    return payment


# Returns
@transaction.atomic
def return_items(*, document_id, business, user, lines, reason=''):
    """lines: list of {'item_id': UUID, 'quantity': Decimal}. Restores stock and
    reduces the invoice balance by each line's share of the invoice total."""
    doc, customer = lock_document(document_id, business)
    _require_sales(doc)
    if doc.status != Document.Status.UNPAID:
        raise DomainError('Items can only be returned on unpaid invoices.', 'invalid_status', 409)
    _require_stock_settled(doc)
    if customer is None:
        raise DomainError('This invoice has no customer attached.', 'customer_required')

    all_items = list(DocumentItem.objects.filter(document=doc).order_by('order', 'id'))
    by_id = {str(i.id): i for i in all_items if i.product_id}

    targets, seen = [], set()
    for line in lines:
        key = str(line['item_id'])
        if key in seen:
            raise DomainError('The same item appears more than once.', 'duplicate_item')
        seen.add(key)
        item = by_id.get(key)
        if item is None:
            raise DomainError('Item not found on this invoice.', 'item_not_found', 404)
        qty = line['quantity']
        returnable = item.quantity_deducted - item.quantity_returned
        if qty > returnable:
            raise DomainError(f"'{item.description}': only {returnable} can be returned.", 'exceeds_returnable')
        targets.append((item, qty))
    if not targets:
        raise DomainError('Choose at least one item to return.', 'invalid_payload')

    needed = defaultdict(Decimal)
    for item, qty in targets:
        needed[item.product_id] += qty
    products = _lock_products(needed, doc.business_id)

    share = (doc.grand_total / doc.subtotal) if doc.subtotal > 0 else Decimal('0')
    created = []
    credit_total = ZERO
    for item, qty in targets:
        credit = money(qty * item.unit_price * share)
        tx = InventoryTransaction.objects.create(
            business_id=doc.business_id, product=products[item.product_id], quantity_change=qty,
            transaction_type=InventoryTransaction.TransactionType.SALES_RETURN,
            reference_document_id=doc.id, initiated_by=user,
            reason=(reason or f'Return on {doc.document_number}')[:255],
        )
        created.append(ItemReturn.objects.create(
            business_id=doc.business_id, document=doc, item=item, quantity=qty, credit_amount=credit,
            reason=reason, inventory_transaction=tx, returned_by=user,
        ))
        item.quantity_returned += qty
        item.save(update_fields=['quantity_returned'])
        credit_total += credit

    for pid, qty in needed.items():
        product = products[pid]
        product.quantity_on_hand += qty
        product.save(update_fields=['quantity_on_hand', 'updated_at'])

    new_credit = doc.returned_credit + credit_total
    # When everything is back, settle the rounding so credit equals the invoice total exactly.
    if all(i.quantity_returned >= i.quantity for i in all_items):
        diff = doc.grand_total - new_credit
        if diff != 0:
            created[-1].credit_amount += diff
            created[-1].save(update_fields=['credit_amount'])
            new_credit += diff

    if new_credit > doc.grand_total - doc.amount_paid:
        raise DomainError(
            'This return is worth more than the balance still owed. '
            'Part of the invoice was already paid, so settle that difference separately.',
            'return_exceeds_balance',
        )

    doc.returned_credit = new_credit
    fields = ['returned_credit', 'updated_at']
    if doc.balance_due <= 0:
        # Nothing left to collect. Revenue is counted net of returns, so this adds nothing to it.
        doc.status = Document.Status.PAID
        doc.paid_at = timezone.now()
        fields += ['status', 'paid_at']
    doc.save(update_fields=fields)
    recalculate_outstanding(customer)
    return doc


# Delete
@transaction.atomic
def soft_delete_document(*, document_id, business):
    doc, customer = lock_document(document_id, business)
    has_payments = PaymentAllocation.objects.filter(document=doc, payment__voided_at__isnull=True).exists()
    if has_payments or ItemReturn.objects.filter(document=doc).exists():
        raise DomainError(
            'This invoice has payments or returns on it. Void the payments and resolve the returns first.',
            'has_ledger_entries', 409,
        )
    doc.soft_delete()
    if customer is not None:
        recalculate_outstanding(customer)
    return doc
import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import ExpressionWrapper, F, Q
from django.utils import timezone


class DocumentManager(models.Manager):
    def get_queryset(self):
        # This forces all queries to exclude soft-deleted items by default
        return super().get_queryset().filter(deleted_at__isnull=True)


def get_receipt_prefix(business_name):
    """Up to four letters: 1 word -> first 3 letters, 2 words -> 2 + 1,
    3+ words -> initials, capped at 4."""
    words = re.findall(r'[^\W\d_]+', business_name or '')   # letters only
    if not words:
        return "INV"

    if len(words) == 1:
        prefix = words[0][:3]
    elif len(words) == 2:
        prefix = words[0][:2] + words[1][:1]
    else:
        prefix = "".join(w[0] for w in words)[:4]
    return prefix.upper()


class Document(models.Model):

    class DocumentType(models.TextChoices):
        PURCHASE_INVOICE = 'purchase_invoice', 'Purchase Invoice'
        PROFORMA_INVOICE = 'proforma_invoice', 'Proforma Invoice'
        SALES_INVOICE    = 'sales_invoice',    'Sales Invoice'

    class Status(models.TextChoices):
        DRAFT     = 'draft',      'Draft'
        PAID      = 'paid',       'Paid'
        UNPAID    = 'unpaid',     'Unpaid'
        DELIVERED = 'delivered',  'Delivered'
        DELETED   = 'deleted',    'Deleted'

    class StockStatus(models.TextChoices):
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'
        PENDING        = 'pending',        'Not deducted'
        PARTIAL        = 'partial',        'Partially deducted'
        DEDUCTED       = 'deducted',       'Deducted'

    # Identity
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)

    # Relations
    business = models.ForeignKey(
        'business.Business',
        on_delete=models.CASCADE,
        related_name='documents',
    )
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='documents',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_documents',
    )

    # Classification
    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    document_number = models.CharField(max_length=20, editable=False)
    document_date   = models.DateField()

    # Denormalised contact info (snapshot at time of creation)
    customer_name  = models.CharField(max_length=255, blank=True)
    customer_email = models.EmailField(blank=True)
    customer_phone = models.CharField(max_length=20, blank=True)

    # Supplier info (purchase invoices)
    supplier_name = models.CharField(max_length=255, blank=True)

    # Financials
    currency = models.CharField(max_length=10, default="₦")
    subtotal      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    tax_rate      = models.DecimalField(max_digits=5,  decimal_places=4, default=Decimal('0.00'))
    tax_amount    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    discount      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    grand_total   = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    # Only the payments service writes amount_paid and returned_credit.
    amount_paid     = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    returned_credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    paid_at     = models.DateTimeField(null=True, blank=True)
    notes       = models.TextField(blank=True)

    # Stock deduction state, derived from DocumentItem.quantity_deducted
    stock_status      = models.CharField(
        max_length=20, choices=StockStatus.choices, default=StockStatus.NOT_APPLICABLE,
    )
    stock_deducted_at = models.DateTimeField(null=True, blank=True)

    # Delivery
    is_delivered  = models.BooleanField(default=False)
    delivered_at  = models.DateTimeField(null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = DocumentManager()
    all_objects = models.Manager()

    class Meta:
        # unique_together = [['business', 'document_number', 'document_type']]
        ordering = ['-document_date', '-created_at']
        indexes = [
            models.Index(fields=['business']),
            models.Index(fields=['document_type']),
            models.Index(fields=['status']),
            models.Index(fields=['created_at']),
            models.Index(fields=['customer', 'status'], name='doc_customer_status_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['business', 'document_number'], name='doc_number_unique_per_business'),
            models.CheckConstraint(condition=Q(amount_paid__gte=0), name='document_amount_paid_gte_0'),
            models.CheckConstraint(condition=Q(returned_credit__gte=0), name='document_returned_credit_gte_0'),
        ]

    # Derived values
    @property
    def balance_due(self):
        return self.grand_total - self.returned_credit - self.amount_paid

    @property
    def is_stock_settled(self):
        """True when no further stock work is expected: everything is deducted,
        nothing is tracked, or the one-time deduction has already been used
        (even if some items were short or out of stock)."""
        return (
            self.stock_status in (self.StockStatus.DEDUCTED, self.StockStatus.NOT_APPLICABLE)
            or self.stock_deducted_at is not None
        )

    def calculate_totals(self):
        """Calculates subtotal, tax_amount, and grand_total based on line items."""
        items_total = self.items.aggregate(models.Sum('total'))['total__sum'] or Decimal('0.00')

        self.subtotal = items_total

        taxable_amount = self.subtotal - self.discount
        if taxable_amount < 0:
            taxable_amount = Decimal('0.00')

        self.tax_amount = taxable_amount * self.tax_rate
        self.grand_total = taxable_amount + self.tax_amount

        self.save(update_fields=['subtotal', 'tax_amount', 'grand_total', 'updated_at'])

    def refresh_stock_status(self):
        """Recomputes stock_status from the item ledger. Call inside the same
        transaction that changed items or deductions."""
        S = self.StockStatus
        if self.document_type != self.DocumentType.SALES_INVOICE:
            new_status = S.NOT_APPLICABLE
        else:
            rows = list(
                self.items.filter(product__isnull=False).values_list('quantity', 'quantity_deducted')
            )
            if not rows:
                new_status = S.NOT_APPLICABLE
            elif all(done >= qty for qty, done in rows):
                new_status = S.DEDUCTED
            elif any(done > 0 for _qty, done in rows):
                new_status = S.PARTIAL
            else:
                new_status = S.PENDING

        fields = []
        if new_status != self.stock_status:
            self.stock_status = new_status
            fields.append('stock_status')
        if new_status == S.DEDUCTED and self.stock_deducted_at is None:
            self.stock_deducted_at = timezone.now()
            fields.append('stock_deducted_at')
        if fields:
            self.save(update_fields=fields + ['updated_at'])
        return self.stock_status

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.status = self.Status.DELETED
        self.save(update_fields=['deleted_at', 'status', 'updated_at'])

    def save(self, *args, **kwargs):
        # Auto-populate currency from business if not already set
        if not self.currency and self.business and hasattr(self.business, 'currency'):
            self.currency = self.business.currency

        if not self.document_number:
            prefix = get_receipt_prefix(self.business.name)

            last_doc = Document.all_objects.filter(
                business=self.business
            ).order_by('-created_at', '-id').first()

            if last_doc and last_doc.document_number:
                match = re.search(r'(\d+)$', last_doc.document_number)
                if match:
                    next_value = int(match.group(1)) + 1
                    self.document_number = f"{prefix}-{next_value:03d}"
                else:
                    self.document_number = f"{prefix}-001"
            else:
                self.document_number = f"{prefix}-001"

        super().save(*args, **kwargs)

    @transaction.atomic
    def mark_delivered(self):
        self.is_delivered = True
        self.delivered_at = timezone.now()
        self.status = self.Status.DELIVERED
        self.save(update_fields=['is_delivered', 'delivered_at', 'status', 'updated_at'])

    def __str__(self):
        return f"{self.get_document_type_display()} #{self.document_number}"


class DocumentItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='document_items',
    )
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    order = models.PositiveIntegerField(default=0)

    # Per-item stock ledger. This is what makes a double deduction impossible:
    # a deduction can never exceed quantity - quantity_deducted.
    quantity_deducted = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'))
    quantity_returned = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'))

    class Meta:
        ordering = ['order', 'id']
        indexes = [models.Index(fields=['document'])]
        constraints = [
            models.CheckConstraint(condition=Q(quantity_deducted__gte=0), name='item_deducted_gte_0'),
            models.CheckConstraint(
                condition=Q(quantity_returned__gte=0, quantity_returned__lte=F('quantity_deducted')),
                name='item_returned_within_deducted',
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.total:
            self.total = self.quantity * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} × {self.quantity}"


# Reusable SQL expressions so every report uses the same definition of
# "what is still owed" and "what counts as revenue".
_MONEY = models.DecimalField(max_digits=12, decimal_places=2)

BALANCE_DUE_EXPR = ExpressionWrapper(
    F('grand_total') - F('returned_credit') - F('amount_paid'), output_field=_MONEY,
)
NET_TOTAL_EXPR = ExpressionWrapper(
    F('grand_total') - F('returned_credit'), output_field=_MONEY,
)











# import re
# import uuid
# from decimal import Decimal
# from django.conf import settings
# from django.core.exceptions import ValidationError
# from django.db import models, transaction
# from django.utils import timezone

# class DocumentManager(models.Manager):
#     def get_queryset(self):
#         # This forces all queries to exclude soft-deleted items by default
#         return super().get_queryset().filter(deleted_at__isnull=True)

# def get_receipt_prefix(business_name):
#     """Derives a prefix based on your JS logic."""
#     words = business_name.strip().split()
#     if not words:
#         return "INV"
    
#     if len(words) == 1:
#         return words[0][:3].upper()
#     elif len(words) == 2:
#         return (words[0][:2] + words[1][:1]).upper()
#     else:
#         return "".join([w[0] for w in words]).upper()


# class Document(models.Model):

#     class DocumentType(models.TextChoices):
#         PURCHASE_INVOICE = 'purchase_invoice', 'Purchase Invoice'
#         PROFORMA_INVOICE = 'proforma_invoice', 'Proforma Invoice'
#         SALES_INVOICE    = 'sales_invoice',    'Sales Invoice'

#     class Status(models.TextChoices):
#         DRAFT     = 'draft',      'Draft'
#         PAID = 'paid',  'Paid'
#         UNPAID = 'unpaid',  'Unpaid'
#         DELIVERED = 'delivered', 'Delivered'
#         DELETED = 'deleted', 'Deleted'
        

#     # ── Identity ──────────────────────────────────────────────────────────────
#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)

#     # ── Relations ─────────────────────────────────────────────────────────────
#     business = models.ForeignKey(
#         'business.Business',
#         on_delete=models.CASCADE,
#         related_name='documents',
#     )
#     customer = models.ForeignKey(
#         'customers.Customer',
#         on_delete=models.SET_NULL,
#         null=True,
#         blank=True,
#         related_name='documents',
#     )
#     created_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.SET_NULL,
#         null=True,
#         related_name='created_documents',
#     )

#     # ── Classification ────────────────────────────────────────────────────────
#     document_type = models.CharField(
#         max_length=20,
#         choices=DocumentType.choices,
#     )
#     status = models.CharField(
#         max_length=20,
#         choices=Status.choices,
#         default=Status.DRAFT,
#     )
#     document_number = models.CharField(max_length=20, unique=True, editable=False)
#     document_date   = models.DateField()
    
#     # ── Denormalised contact info (snapshot at time of creation) ──────────────
#     customer_name  = models.CharField(max_length=255, blank=True)
#     customer_email = models.EmailField(blank=True)
#     customer_phone = models.CharField(max_length=20, blank=True)

#     # ── Supplier info (purchase invoices) ─────────────────────────────────────
#     supplier_name = models.CharField(max_length=255, blank=True)

#     # ── Financials ────────────────────────────────────────────────────────────
#     currency = models.CharField(max_length=10, default="₦") 
#     subtotal      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
#     tax_rate      = models.DecimalField(max_digits=5,  decimal_places=4, default=Decimal('0.00'))
#     tax_amount    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
#     discount      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
#     grand_total   = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

#     amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
#     paid_at     = models.DateTimeField(null=True, blank=True)
#     notes       = models.TextField(blank=True)
#     # ── Delivery ──────────────────────────────────────────────────────────────
#     is_delivered  = models.BooleanField(default=False)
#     delivered_at  = models.DateTimeField(null=True, blank=True)

   
#     deleted_at = models.DateTimeField(null=True, blank=True)

#     # ── Timestamps ────────────────────────────────────────────────────────────
#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)
    
#     objects = DocumentManager()
    
#     all_objects = models.Manager()
    

#     class Meta:
#         unique_together = [['business', 'document_number', 'document_type']]
#         ordering = ['-document_date', '-created_at']
#         indexes = [
#             models.Index(fields=['business']),
#             models.Index(fields=['document_type']),
#             models.Index(fields=['status']),
#             models.Index(fields=['created_at']),
#         ]

#     # ── Helpers ───────────────────────────────────────────────────────────────
    
#     def calculate_totals(self):
#         """
#         Calculates subtotal, tax_amount, and grand_total based on line items.
#         """
#         items_total = self.items.aggregate(models.Sum('total'))['total__sum'] or Decimal('0.00')
        
#         self.subtotal = items_total
        
#         taxable_amount = self.subtotal - self.discount
#         if taxable_amount < 0: taxable_amount = Decimal('0.00')
        
#         self.tax_amount = taxable_amount * self.tax_rate
#         self.grand_total = taxable_amount + self.tax_amount
        
#         self.save(update_fields=['subtotal', 'tax_amount', 'grand_total', 'updated_at'])
    
#     def soft_delete(self):
#         self.deleted_at = timezone.now()
#         self.status = self.Status.DELETED
#         self.save(update_fields=['deleted_at', 'status', 'updated_at'])

#     def mark_paid(self):
#         self.status = self.Status.PAID
#         self.paid_at = timezone.now()
#         self.save(update_fields=['status', 'paid_at', 'updated_at'])
        
#     def save(self, *args, **kwargs):
#         # Auto-populate currency from business if not already set
#         if not self.currency and self.business and hasattr(self.business, 'currency'):
#             self.currency = self.business.currency

#         if not self.document_number:
#             prefix = get_receipt_prefix(self.business.name)
            
#             last_doc = Document.objects.filter(
#                 business=self.business
#             ).order_by('-created_at', '-id').first()

#             if last_doc and last_doc.document_number:
#                 match = re.search(r'(\d+)$', last_doc.document_number)
#                 if match:
#                     next_value = int(match.group(1)) + 1
#                     self.document_number = f"{prefix}-{next_value:03d}"
#                 else:
#                     self.document_number = f"{prefix}-001"
#             else:
#                 self.document_number = f"{prefix}-001"
                
#         super().save(*args, **kwargs)

#     # ── Document lifecycle ────────────────────────────────────────────────────


#     @transaction.atomic
#     def mark_delivered(self):
#         self.is_delivered = True
#         self.delivered_at = timezone.now()
#         self.status = self.Status.DELIVERED
#         self.save(update_fields=['is_delivered', 'delivered_at', 'status', 'updated_at'])


#     def __str__(self):
#         return f"{self.get_document_type_display()} #{self.document_number}"


# class DocumentItem(models.Model):
#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     document = models.ForeignKey(
#         Document,
#         on_delete=models.CASCADE,
#         related_name='items',
#     )
#     product = models.ForeignKey(
#         'products.Product',
#         on_delete=models.SET_NULL,
#         null=True,
#         blank=True,
#         related_name='document_items',
#     )
#     description = models.CharField(max_length=255)
#     quantity = models.DecimalField(max_digits=10, decimal_places=3)
#     unit_price = models.DecimalField(max_digits=12, decimal_places=2)
#     total = models.DecimalField(max_digits=12, decimal_places=2)
#     order = models.PositiveIntegerField(default=0)

#     class Meta:
#         ordering = ['order', 'id']
#         indexes = [models.Index(fields=['document'])]

#     def save(self, *args, **kwargs):
#         if not self.total:
#             self.total = self.quantity * self.unit_price
#         super().save(*args, **kwargs)

#     def __str__(self):
#         return f"{self.description} × {self.quantity}"

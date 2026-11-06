from django.db import models
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Payment(models.Model):
    """
    Money received from a customer. It is never edited or deleted: a mistake
    is corrected by voiding it, which keeps the full audit trail.
    """

    class Method(models.TextChoices):
        CASH = 'cash', 'Cash'
        BANK_TRANSFER = 'bank_transfer', 'Bank transfer'
        CARD = 'card', 'Card'
        CHECK = 'check', 'Check'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey('business.Business', on_delete=models.CASCADE, related_name='payments')
    customer = models.ForeignKey('customers.Customer', on_delete=models.RESTRICT, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=255, blank=True)
    # Sent by the client so a retried request after a network drop cannot
    # record the same payment twice.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='recorded_payments',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='voided_payments',
    )
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', '-created_at'], name='payment_cust_created_idx'),
            models.Index(fields=['business', 'created_at'], name='payment_biz_created_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='payment_amount_positive'),
            models.UniqueConstraint(
                fields=['business', 'idempotency_key'],
                condition=Q(idempotency_key__isnull=False),
                name='payment_unique_idempotency_key',
            ),
        ]

    def __str__(self):
        return f'Payment {self.amount} from {self.customer_id}'


class PaymentAllocation(models.Model):
    """The slice of a payment applied to one invoice."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='allocations')
    document = models.ForeignKey('documents.Document', on_delete=models.RESTRICT, related_name='payment_allocations')
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        indexes = [models.Index(fields=['document'], name='alloc_document_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='allocation_amount_positive'),
            models.UniqueConstraint(fields=['payment', 'document'], name='allocation_unique_payment_document'),
        ]


class ItemReturn(models.Model):
    """Goods a customer sent back from an unpaid invoice."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey('business.Business', on_delete=models.CASCADE, related_name='item_returns')
    document = models.ForeignKey('documents.Document', on_delete=models.RESTRICT, related_name='item_returns')
    item = models.ForeignKey('documents.DocumentItem', on_delete=models.RESTRICT, related_name='returns')
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    credit_amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True)
    inventory_transaction = models.OneToOneField(
        'inventory.InventoryTransaction', on_delete=models.RESTRICT, null=True, blank=True, related_name='item_return',
    )
    returned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='item_returns',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['document'], name='return_document_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name='return_quantity_positive'),
            models.CheckConstraint(condition=Q(credit_amount__gte=0), name='return_credit_gte_0'),
        ]

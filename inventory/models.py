import uuid
from django.conf import settings
from django.db import models


class InventoryTransaction(models.Model):

    class TransactionType(models.TextChoices):

        PURCHASE_RECEIVED = 'purchase_received', 'Purchase Received'
        ADJUSTMENT        = 'adjustment',        'Adjustment'
        SALES_CONFIRMED   = 'sales_confirmed',   'Sales Confirmed'
        SALES_RETURN      = 'sales_return',      'Sales Return'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    business = models.ForeignKey(
        'business.Business',
        on_delete=models.CASCADE,
        related_name='inventory_transactions',
    )
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.CASCADE,
        related_name='inventory_transactions',
    )
    quantity_change = models.DecimalField(max_digits=10, decimal_places=3)
    transaction_type = models.CharField(
        max_length=20,
        choices=TransactionType.choices,
    )
    reference_document_id = models.UUIDField(null=True, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='inventory_transactions',
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['product', 'created_at']),
            models.Index(fields=['business', 'created_at']),
        ]

    def __str__(self):
        direction = '+' if self.quantity_change >= 0 else ''
        return f"{self.get_transaction_type_display()} {direction}{self.quantity_change} - {self.product}"
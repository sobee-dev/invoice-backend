from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from django.utils import timezone
import uuid

class SyncStatus(models.TextChoices):
    """Sync status choices"""
    PENDING = 'pending', 'Pending'
    SYNCED = 'synced', 'Synced'
    ERROR = 'error', 'Error'
    DELETED = 'deleted', 'Deleted'
    
    
class Template(models.Model):
    """Receipt templates - matches TypeScript Template interface"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField()
    preview_image = models.CharField(max_length=500)  # snake_case: previewImage -> preview_image
    
    # System or user template
    is_system = models.BooleanField(default=True) # snake_case: isSystem -> is_system
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='templates'
    )

    class Meta:
        db_table = 'templates'
        ordering = ['name']

    def __str__(self):
        return self.name    


class Receipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    business = models.ForeignKey(
        'business.Business', 
        on_delete=models.CASCADE, 
        related_name='receipts'
    )
    
    template = models.ForeignKey(
        Template,
        on_delete=models.SET_NULL,
        null=True,
        related_name='receipts'
    )
    
    receipt_number = models.CharField(max_length=50) # receiptNumber -> receipt_number
    receipt_date = models.DateTimeField() # receiptDate -> receipt_date
    
    # Customer information
    customer_name = models.CharField(max_length=255) # customerName -> customer_name
    customer_phone = models.CharField(max_length=20, blank=True, null=True) # customerPhone -> customer_phone
    customer_email = models.EmailField(blank=True, null=True) # customerEmail -> customer_email
    
    # Financial details
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('1'))]
    ) # taxRate -> tax_rate
    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    ) # taxAmount -> tax_amount
    discount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    grand_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    ) # grandTotal -> grand_total
    
    # Additional information
    notes = models.TextField(blank=True, null=True)
    
    # Payment status
    is_paid = models.BooleanField(default=False) # isPaid -> is_paid
    paid_at = models.DateTimeField(null=True, blank=True) # paidAt -> paid_at
    
    # Sync fields
    server_id = models.IntegerField(null=True, blank=True, unique=True) # serverId -> server_id
    sync_status = models.CharField(
        max_length=20,
        choices=SyncStatus.choices,
        default=SyncStatus.PENDING
    ) # syncStatus -> sync_status
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True) # createdAt -> created_at
    updated_at = models.DateTimeField(auto_now=True) # updatedAt -> updated_at
    deleted_at = models.DateTimeField(null=True, blank=True) # deletedAt -> deleted_at

    class Meta:
        db_table = 'receipts'
        ordering = ['-receipt_date', '-created_at']
        indexes = [
            models.Index(fields=['business']),
            models.Index(fields=['receipt_number']),
            models.Index(fields=['sync_status']),
            models.Index(fields=['server_id']),
            models.Index(fields=['-receipt_date']),
            models.Index(fields=['is_paid']),
        ]
        unique_together = [['business', 'receipt_number']]

    def __str__(self):
        return f"{self.receipt_number} - {self.customer_name}"

    def mark_as_paid(self):
        self.is_paid = True
        self.paid_at = timezone.now()
        self.save(update_fields=['is_paid', 'paid_at', 'updated_at'])

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.sync_status = SyncStatus.DELETED
        self.save(update_fields=['deleted_at', 'sync_status', 'updated_at'])


class ReceiptItem(models.Model):
    """Receipt line items - matches TypeScript ReceiptItem interface"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.CASCADE,
        related_name='items'
    )
    
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))]
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    ) # unitPrice -> unit_price
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    
    # Order for display
    order = models.PositiveIntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True) # createdAt -> created_at
    updated_at = models.DateTimeField(auto_now=True) # updatedAt -> updated_at

    class Meta:
        db_table = 'receipt_items'
        ordering = ['order', 'id']
        indexes = [
            models.Index(fields=['receipt']),
        ]

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    def save(self, *args, **kwargs):
        """Auto-calculate total if not provided"""
        if not self.total:
            self.total = self.quantity * self.unit_price
        super().save(*args, **kwargs)
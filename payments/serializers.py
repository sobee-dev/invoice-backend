from decimal import Decimal

from rest_framework import serializers

from documents.models import Document, DocumentItem
from .models import ItemReturn, Payment, PaymentAllocation

METHOD_CHOICES = Payment.Method.choices


# Input
class RecordPaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=METHOD_CHOICES, default=Payment.Method.CASH)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    document_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    idempotency_key = serializers.RegexField(
        r'^[A-Za-z0-9_-]{8,64}$', required=False, allow_null=True, default=None,
        error_messages={'invalid': 'Use 8 to 64 letters, numbers, dashes or underscores.'},
    )


class MarkPaidSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=METHOD_CHOICES, default=Payment.Method.CASH)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')


class MarkUnpaidSerializer(serializers.Serializer):
    deduct_stock = serializers.BooleanField(default=False)


class ReturnLineSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=10, decimal_places=3, min_value=Decimal('0.001'))


class ReturnItemsSerializer(serializers.Serializer):
    items = ReturnLineSerializer(many=True, allow_empty=False, max_length=100)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')


class VoidPaymentSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


# Output
def _person(user):
    if user is None:
        return None
    return (user.first_name or '').strip() or user.email


class PaymentAllocationSerializer(serializers.ModelSerializer):
    document_number = serializers.CharField(source='document.document_number', read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ['id', 'document', 'document_number', 'amount']


class PaymentSerializer(serializers.ModelSerializer):
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            'id', 'customer', 'amount', 'method', 'reference', 'note', 'created_at',
            'voided_at', 'void_reason', 'recorded_by_name', 'allocations',
        ]

    def get_recorded_by_name(self, obj):
        return _person(obj.recorded_by)


class InvoiceSummarySerializer(serializers.ModelSerializer):
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'document_number', 'document_date', 'customer', 'customer_name', 'currency',
            'status', 'stock_status', 'grand_total', 'returned_credit', 'amount_paid', 'balance_due',
        ]


class InvoiceItemSerializer(serializers.ModelSerializer):
    returnable = serializers.SerializerMethodField()

    class Meta:
        model = DocumentItem
        fields = [
            'id', 'product', 'description', 'quantity', 'unit_price', 'total',
            'quantity_deducted', 'quantity_returned', 'returnable',
        ]

    def get_returnable(self, obj):
        return obj.quantity_deducted - obj.quantity_returned


class InvoiceAllocationSerializer(serializers.ModelSerializer):
    payment = serializers.UUIDField(source='payment_id', read_only=True)
    method = serializers.CharField(source='payment.method', read_only=True)
    reference = serializers.CharField(source='payment.reference', read_only=True)
    created_at = serializers.DateTimeField(source='payment.created_at', read_only=True)
    voided_at = serializers.DateTimeField(source='payment.voided_at', read_only=True)
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PaymentAllocation
        fields = ['id', 'payment', 'amount', 'method', 'reference', 'created_at', 'voided_at', 'recorded_by_name']

    def get_recorded_by_name(self, obj):
        return _person(obj.payment.recorded_by)


class ItemReturnSerializer(serializers.ModelSerializer):
    description = serializers.CharField(source='item.description', read_only=True)

    class Meta:
        model = ItemReturn
        fields = ['id', 'item', 'description', 'quantity', 'credit_amount', 'reason', 'created_at']


class OutstandingInvoiceSerializer(serializers.ModelSerializer):
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    days_outstanding = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id', 'document_number', 'document_date', 'customer', 'customer_name', 'currency',
            'stock_status', 'grand_total', 'returned_credit', 'amount_paid', 'balance_due', 'days_outstanding',
        ]

    def get_days_outstanding(self, obj):
        from django.utils import timezone
        return max((timezone.localdate() - obj.document_date).days, 0)
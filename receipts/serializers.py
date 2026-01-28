from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator
from decimal import Decimal
from .models import Receipt, ReceiptItem, Template

class ReceiptItemSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)

    class Meta:
        model = ReceiptItem
        fields = ['id', 'description', 'quantity', 'unit_price', 'total', 'order']
        read_only_fields = ['total']


class ReceiptDetailSerializer(serializers.ModelSerializer):
    items = ReceiptItemSerializer(many=True)
    
    class Meta:
        model = Receipt
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'server_id']
        # Explicitly handling the unique constraint for the API
        validators = [
            UniqueTogetherValidator(
                queryset=Receipt.objects.all(),
                fields=['business', 'receipt_number'],
                message="This receipt number already exists for this business."
            )
        ]

    def validate(self, data):
        """
        Cross-field validation to ensure financial integrity.
        """
        items = data.get('items', [])
        subtotal = data.get('subtotal', Decimal('0.00'))
        tax_rate = data.get('tax_rate', Decimal('0.00'))
        discount = data.get('discount', Decimal('0.00'))
        grand_total = data.get('grand_total', Decimal('0.00'))

        # 1. Verify Subtotal (Sum of items)
        calculated_subtotal = sum(
            Decimal(str(item.get('quantity', 0))) * Decimal(str(item.get('unit_price', 0))) 
            for item in items
        )
        
        if abs(calculated_subtotal - subtotal) > Decimal('0.01'):
            raise serializers.ValidationError({
                "subtotal": f"Subtotal mismatch. Expected {calculated_subtotal}, got {subtotal}"
            })

        # 2. Verify Grand Total: (Subtotal + (Subtotal * Tax)) - Discount
        tax_amount = (subtotal * tax_rate).quantize(Decimal('0.01'))
        expected_grand_total = (subtotal + tax_amount - discount).quantize(Decimal('0.01'))

        if abs(expected_grand_total - grand_total) > Decimal('0.01'):
            raise serializers.ValidationError({
                "grand_total": f"Grand total integrity check failed. Expected {expected_grand_total} based on items and taxes."
            })

        return data

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        receipt = Receipt.objects.create(**validated_data)
        
        for item_data in items_data:
            # We don't need to pass 'total' as the model's save() handles it
            ReceiptItem.objects.create(receipt=receipt, **item_data)
            
        return receipt

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if items_data is not None:
            # Atomic update: remove old items and add new ones
            instance.items.all().delete()
            for item_data in items_data:
                item_data.pop('id', None) # Remove ID if present in update payload
                ReceiptItem.objects.create(receipt=instance, **item_data)
        
        return instance


class ReceiptListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for dashboard tables."""
    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'receipt_date', 
            'customer_name', 'grand_total', 'is_paid', 'sync_status'
        ]
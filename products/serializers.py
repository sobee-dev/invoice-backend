from rest_framework import serializers

from receipt_backend_api import settings
from .models import Product
from decimal import Decimal



def validate_cloudinary_image_url(value):
    """
    Shared with business/serializers.py's validate_logo_url — same rule,
    same reason: purge_account() calls delete_cloudinary_asset() on this
    field, so anything that isn't actually hosted on our Cloudinary account
    would either fail silently or, worse, attempt to "delete" a URL we
    don't own.
    """
    if not value:
        return value
    cloud_name = settings.CLOUDINARY_STORAGE["CLOUD_NAME"]
    if f"res.cloudinary.com/{cloud_name}/" not in value:
        raise serializers.ValidationError("Product image must be uploaded via Cloudinary")
    return value

class ProductSerializer(serializers.ModelSerializer):
    available_to_sell = serializers.DecimalField(
        max_digits=10, decimal_places=3, read_only=True
    )
    is_low_stock = serializers.BooleanField(read_only=True)
    total_sold = serializers.DecimalField(max_digits=10, decimal_places=3, read_only=True)
    
    class Meta:
        model = Product
        fields = [
            'id', 'business', 'name', 'description', 'sku',
            'unit_price', 'image_url',
            'quantity_on_hand', 'quantity_reserved', 'reorder_level',
            'available_to_sell', 'total_sold', 'is_low_stock',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id','total_sold', 'business', 'created_at', 'updated_at']

    def validate_image_url(self, value):
        return validate_cloudinary_image_url(value)

class ProductUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            'name', 'description', 'sku', 'unit_price',
            'image_url', 'reorder_level', 'is_active'
        ]
        
    def validate_image_url(self, value):
        return validate_cloudinary_image_url(value)    

    def validate_sku(self, value):
        if not value:
            return value

        business = self.instance.business
        conflict = Product.objects.filter(
            business=business, sku=value
        ).exclude(pk=self.instance.pk)

        if conflict.exists():
            raise serializers.ValidationError('A product with this SKU already exists.')

        return value

class ProductListSerializer(serializers.ModelSerializer):
    total_sold = serializers.DecimalField(max_digits=10, decimal_places=3, source='total_sold_qty')
    available_to_sell = serializers.DecimalField(max_digits=10, decimal_places=3, source='available_stock')
    is_low_stock = serializers.BooleanField(source='low_stock_flag')

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'sku', 'unit_price', 'image_url',
            'quantity_on_hand', 'total_sold', 'available_to_sell', 'is_low_stock',
            'is_active','reorder_level',
        ]

class StockAdjustmentSerializer(serializers.Serializer):
    """
    Input serializer for POST /api/products/{id}/adjust-stock/.
    Not a ModelSerializer — this doesn't map 1:1 to InventoryTransaction,
    since the view derives transaction_type, business, and initiated_by itself.
    """
    quantity_change = serializers.DecimalField(max_digits=10, decimal_places=3)
    reason = serializers.CharField(max_length=255, allow_blank=False, trim_whitespace=True)
    reference_document_id = serializers.UUIDField(required=False, allow_null=True)
    
    def validate_quantity_change(self, value):
        if value == 0:
            raise serializers.ValidationError('Quantity change cannot be zero.')
        return value

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('A reason is required for stock adjustments.')
        return value
    
    
class BulkDeductItemSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=10, decimal_places=3, min_value=Decimal('0.001'))
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')


class BulkDeductSerializer(serializers.Serializer):
    reference_document_id = serializers.UUIDField(required=False, allow_null=True)
    items = BulkDeductItemSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('At least one item is required.')
        return value    
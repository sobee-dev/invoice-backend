# documents/serializers.py
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from business.utils import get_user_business
from customers.services import get_or_create_customer
from products.services import assert_items_within_plan, get_or_create_product
from .models import Document, DocumentItem


def _assert_customer_in_business(customer, business):
    if customer is not None and customer.business_id != business.id:
        raise serializers.ValidationError({'customer': 'This customer does not belong to your business.'})


def _assert_product_in_business(product, business):
    if product is not None and product.business_id != business.id:
        raise serializers.ValidationError({'items': 'One of the products does not belong to your business.'})


class DocumentUserSerializer(serializers.ModelSerializer):
    """
    Nested read-only representation of the user who created a document.
    display_name folds the fallback in server-side: first_name if set,
    otherwise email. Frontend only needs to layer "Me" on top by
    comparing id against the logged-in user.
    """
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ['id', 'first_name', 'email', 'display_name']

    def get_display_name(self, obj):
        if obj.first_name and obj.first_name.strip():
            return obj.first_name.strip()
        return obj.email


class DocumentItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    product_image_url = serializers.SerializerMethodField()

    class Meta:
        model = DocumentItem
        fields = [
            'id', 'product', 'product_name', 'description', 'quantity', 'unit_price', 'total',
            'product_image_url', 'quantity_deducted', 'quantity_returned',
        ]
        extra_kwargs = {
            'product': {'required': False},
            'id': {'read_only': True},
            'total': {'read_only': True},
            'quantity_deducted': {'read_only': True},
            'quantity_returned': {'read_only': True},
            'quantity': {'min_value': Decimal('0.001')},
            'unit_price': {'min_value': Decimal('0')},
        }

    def get_product_image_url(self, obj):
        if obj.product and obj.product.image_url:
            return obj.product.image_url
        return None

    def validate(self, attrs):
        if not attrs.get('product') and not attrs.get('product_name'):
            raise serializers.ValidationError('Either product or product_name is required.')
        return attrs


class DocumentSerializer(serializers.ModelSerializer):
    created_by = DocumentUserSerializer(read_only=True)
    items = DocumentItemSerializer(many=True, required=False)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Document
        fields = '__all__'
        # Status, money and stock fields are server-controlled. A client must
        # never be able to create a document that is already "paid".
        read_only_fields = [
            'id', 'business', 'created_by', 'created_at', 'updated_at', 'document_number',
            'status', 'subtotal', 'tax_amount', 'grand_total',
            'amount_paid', 'returned_credit', 'paid_at',
            'stock_status', 'stock_deducted_at',
            'is_delivered', 'delivered_at', 'deleted_at',
        ]
        validators = []
        extra_kwargs = {
            'discount': {'min_value': Decimal('0')},
            'tax_rate': {'min_value': Decimal('0'), 'max_value': Decimal('1')},
        }

    def validate(self, attrs):
        # A line that uses a product outside the plan's window is refused here,
        # with the error attached to that line.
        request = self.context.get('request')
        business = get_user_business(request.user) if request else None
        items = attrs.get('items')
        if business is not None and items:
            assert_items_within_plan(business, items)
        return attrs

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        business = validated_data['business']

        _assert_customer_in_business(validated_data.get('customer'), business)
        for item_data in items_data:
            _assert_product_in_business(item_data.get('product'), business)

        with transaction.atomic():
            if not validated_data.get('customer'):
                name = validated_data.get('customer_name')
                email = validated_data.get('customer_email')
                phone = validated_data.get('customer_phone')
                if name or email:
                    validated_data['customer'] = get_or_create_customer(
                        business=business, name=name, email=email, phone=phone,
                    )

            document = Document.objects.create(**validated_data)

            for item_data in items_data:
                product_name = item_data.pop('product_name', None)
                if not item_data.get('product') and product_name:
                    unit_price = item_data.get('unit_price', 0)
                    item_data['product'] = get_or_create_product(document.business, product_name, unit_price=unit_price)

                DocumentItem.objects.create(document=document, **item_data)

            document.calculate_totals()
            document.refresh_stock_status()

        return document


class DocumentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for paginated list views."""
    created_by = DocumentUserSerializer(read_only=True)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'document_type', 'status', 'document_number', 'document_date',
            'customer', 'customer_name', 'supplier_name', 'currency',
            'grand_total', 'amount_paid', 'returned_credit', 'balance_due', 'stock_deducted_at',
            'stock_status', 'is_delivered', 'created_by', 'created_at',
        ]


class RecentActivitySerializer(serializers.ModelSerializer):
    created_by = DocumentUserSerializer(read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'document_number', 'document_type',
            'customer_name', 'supplier_name', 'grand_total', 'currency', 'stock_deducted_at',
            'status', 'stock_status', 'created_at', 'created_by',
        ]


class DocumentUpdateSerializer(serializers.ModelSerializer):
    """
    Updates the editable content of a draft. Status, payment and delivery
    fields are deliberately NOT here: they change only through the dedicated
    actions (mark-paid, mark-unpaid, deliver) so the ledger stays consistent.
    """
    items = DocumentItemSerializer(many=True, required=False)

    class Meta:
        model = Document
        fields = [
            'customer_name', 'customer_email', 'customer_phone', 'supplier_name',
            'items', 'currency', 'notes', 'discount', 'tax_rate',
        ]
        extra_kwargs = {
            'discount': {'min_value': Decimal('0')},
            'tax_rate': {'min_value': Decimal('0'), 'max_value': Decimal('1')},
        }

    def validate(self, attrs):
        items = attrs.get('items')
        if items:
            assert_items_within_plan(self.instance.business, items)
        return attrs

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)

        if items_data is not None:
            for item_data in items_data:
                _assert_product_in_business(item_data.get('product'), instance.business)

        contact_fields_touched = any(
            f in validated_data for f in ('customer_name', 'customer_email', 'customer_phone')
        )
        if contact_fields_touched:
            name = validated_data.get('customer_name', instance.customer_name)
            email = validated_data.get('customer_email', instance.customer_email)
            phone = validated_data.get('customer_phone', instance.customer_phone)
            if name or email:
                instance.customer = get_or_create_customer(
                    business=instance.business, name=name, email=email, phone=phone,
                )

        instance = super().update(instance, validated_data)

        if items_data is not None:
            instance.items.all().delete()
            for item_data in items_data:
                product_name = item_data.pop('product_name', None)
                if not item_data.get('product') and product_name:
                    unit_price = item_data.get('unit_price', 0)
                    item_data['product'] = get_or_create_product(instance.business, product_name, unit_price=unit_price)
                DocumentItem.objects.create(document=instance, **item_data)

        instance.calculate_totals()
        instance.refresh_stock_status()
        return instance
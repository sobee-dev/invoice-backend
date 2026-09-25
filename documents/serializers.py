from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from customers.services import get_or_create_customer
from .models import Document, DocumentItem
from products.services import get_or_create_product


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
        fields = ['id', 'product', 'product_name', 'description', 'quantity', 'unit_price', 'total', 'product_image_url']
        extra_kwargs = {
            'product': {'required': False},
            'id': {'read_only': True},
            'total': {'read_only': True},
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

    class Meta:
        model = Document
        fields = '__all__'
        read_only_fields = ['id', 'business', 'created_by', 'created_at', 'updated_at', 'document_number']
        validators = []

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])

        with transaction.atomic():
            if not validated_data.get('customer'):
                name = validated_data.get('customer_name')
                email = validated_data.get('customer_email')
                phone = validated_data.get('customer_phone')
                if name or email:
                    validated_data['customer'] = get_or_create_customer(
                        business=validated_data['business'],
                        name=name,
                        email=email,
                        phone=phone,
                    )

            document = Document.objects.create(**validated_data)

            for item_data in items_data:
                product_name = item_data.pop('product_name', None)
                if not item_data.get('product') and product_name:
                    unit_price = item_data.get('unit_price', 0)
                    item_data['product'] = get_or_create_product(document.business, product_name, unit_price=unit_price)

                DocumentItem.objects.create(document=document, **item_data)

            document.calculate_totals()
            document.save()

        return document


class DocumentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for paginated list views."""
    created_by = DocumentUserSerializer(read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'document_type', 'status', 'document_number', 'document_date',
            'customer_name', 'supplier_name', 'currency',
            'grand_total', 'amount_paid',
            'is_delivered', 'created_by', 'created_at' ,
        ]


class RecentActivitySerializer(serializers.ModelSerializer):
    created_by = DocumentUserSerializer(read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'document_number', 'document_type',
            'customer_name', 'grand_total', 'currency',
            'status', 'created_at', 'created_by',
        ]


class DocumentUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for updating specific document fields.
    Restricts sensitive fields like business, totals, and creation details.
    """
    items = DocumentItemSerializer(many=True, required=False)

    class Meta:
        model = Document
        fields = [
            'status', 'customer_name', 'customer_email', 'customer_phone', 'items', 'currency', 'notes',
            'discount', 'tax_rate', 'is_delivered', 'delivered_at', 'amount_paid', 'paid_at'
        ]

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)

        contact_fields_touched = any(
            f in validated_data for f in ('customer_name', 'customer_email', 'customer_phone')
        )
        if contact_fields_touched:
            name = validated_data.get('customer_name', instance.customer_name)
            email = validated_data.get('customer_email', instance.customer_email)
            phone = validated_data.get('customer_phone', instance.customer_phone)
            if name or email:
                instance.customer = get_or_create_customer(
                    business=instance.business,
                    name=name,
                    email=email,
                    phone=phone,
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
        instance.save()
        return instance
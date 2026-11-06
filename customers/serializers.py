from decimal import Decimal

from rest_framework import serializers

from .models import Customer


class CustomerSerializer(serializers.ModelSerializer):
    lifetime_value = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            'id', 'business',
            'full_name', 'email', 'phone',
            'payment_method_preference', 'tags', 'notes',
            'outstanding_balance', 'lifetime_value',
            'status', 'created_at',
        ]
        # outstanding_balance is derived from unpaid invoices by the payments
        # service. A client must never be able to write it.
        read_only_fields = ['id', 'business', 'created_at', 'outstanding_balance']

    def get_lifetime_value(self, obj):
        annotated = getattr(obj, 'ltv', None)
        if annotated is not None:
            return annotated
        return obj.lifetime_value


class CustomerListSerializer(serializers.ModelSerializer):
    lifetime_value = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=Decimal('0.00'))

    class Meta:
        model = Customer
        fields = [
            'id', 'full_name', 'email', 'phone', 'lifetime_value',
            'tags', 'outstanding_balance', 'status', 'created_at',
        ]
        read_only_fields = fields
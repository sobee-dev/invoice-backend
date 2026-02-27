from rest_framework import serializers
from .models import Business, SyncStatus
from accounts.models import User


# --- BASE SERIALIZER FOR SHARED VALIDATION ---
class BusinessBaseSerializer(serializers.ModelSerializer):
    """
    Parent class to house all validation logic so it's shared 
    between Create, Update, and General serializers.
    """
    owner = serializers.ReadOnlyField(source='owner.email')

    class Meta:
        model = Business
        fields = '__all__'
        read_only_fields = ['id', 'owner', 'created_at']

    def validate_tax_rate(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Tax rate must be between 0 and 100")
        return value

    def validate_phone(self, value):
        if not value:
            raise serializers.ValidationError("Phone number is required")
        cleaned = value.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        if not cleaned.isdigit():
            raise serializers.ValidationError("Phone number must contain only digits and common formatting characters")
        if len(cleaned) < 7 or len(cleaned) > 15:
            raise serializers.ValidationError("Phone number must be between 7 and 15 digits")
        return value

    def validate_email(self, value):
        return value.lower() if value else value

    def validate_currency(self, value):
        if value and len(value) != 3:
            raise serializers.ValidationError("Currency code must be 3 characters (ISO 4217)")
        return value.upper() if value else value

    def validate_logo_url(self, value):
        if not value:
            return value
        if "res.cloudinary.com" not in value:
            raise serializers.ValidationError("Logo must be uploaded via Cloudinary")
        # allowed_ext = (".png", ".jpg", ".jpeg", ".webp", ".svg")
        # if not any(ext in value.lower() for ext in allowed_ext):
        #     raise serializers.ValidationError("Unsupported logo image format")
        return value

    def validate_signatures(self, attrs):
        """Shared logic for signature validation"""
        sig_type = attrs.get("signature_type", getattr(self.instance, 'signature_type', None))
        sig_text = attrs.get("signature_text", getattr(self.instance, 'signature_text', None))
        sig_url = attrs.get("signature_url", getattr(self.instance, 'signature_url', None))

        if sig_type == "text" and not sig_text:
            raise serializers.ValidationError({"signature_text": "Text signature is required"})
        if sig_type == "image" and not sig_url:
            raise serializers.ValidationError({"signature_url": "Signature image is required"})
        if sig_type == "none":
            attrs["signature_text"] = ""
            attrs["signature_url"] = ""
        return attrs

class BusinessSerializer(serializers.ModelSerializer):
    
    class Meta(BusinessBaseSerializer.Meta):
        # model = Business
        fields = [
            'id',
            'owner',
            'name',
            'description',
            'address_one',
            'address_two',
            'phone',
            'email',
            'registration_number',
            'logo_url',
            'brand_color_one',
            'brand_color_two',
            'currency',
            'tax_rate',
            'tax_enabled',
            'selected_template_id',
            'onboarding_complete',
            'motto',
            'server_id',
            'signature_type',
            'signature_text',
            'signature_url',
            'sync_status',
            'created_at',
        ]
        read_only_fields = ['id', 'sync_status', 'created_at', 'server_id']
 

class BusinessListSerializer(BusinessBaseSerializer):
    """
    Lightweight serializer for listing businesses
    """
    owner_name = serializers.SerializerMethodField()
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    sync_status_display = serializers.CharField(source='get_sync_status_display', read_only=True)
    
    class Meta:
        model = Business
        fields = [
            'id',
            'name',
            'owner_name',
            'owner_email',
            'email',
            'phone',
            'logo',
            'onboarding_complete',
            'sync_status',
            'sync_status_display',
            'created_at',
        ]
        read_only_fields = fields
    
    def get_owner_name(self, obj):
        """Get owner's name"""
        full_name = f"{obj.owner.first_name} {obj.owner.last_name}".strip()
        return full_name if full_name else obj.owner.email


class BusinessCreateSerializer(BusinessBaseSerializer):
    """
    Serializer specifically for creating a new business
    """
    class Meta(BusinessBaseSerializer.Meta):
        # model = Business
        fields = [
            'name',
            'description',
            'address_one',
            'address_two',
            'phone',
            'email',
            'registration_number',
            'logo_url',
            'brand_color_one',
            'brand_color_two',
            'currency',
            'tax_rate',
            'tax_enabled',
            'selected_template_id',
            'motto',
            'signature_url',
            'signature_text',
            'signature_type',
            'onboarding_complete',
        ]
        extra_kwargs = {
            'description': {'required': False, 'allow_blank': True},
            'address_two': {'required': False, 'allow_blank': True, 'allow_null': True},
            'registration_number': {'required': False, 'allow_blank': True, 'allow_null': True},
            'logo_url': {'required': False, 'allow_null': True},
            'motto': {'required': False, 'allow_blank': True, 'allow_null': True},
            'signature_url': {'required': False, 'allow_blank': True},
        }
        
    def validate(self, attrs):
        request = self.context.get('request')
        if request and Business.objects.filter(owner=request.user).exists():
            raise serializers.ValidationError('You already have a business registered')
        return self.validate_signatures(attrs)

    def create(self, validated_data):
        validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)

class BusinessUpdateSerializer(BusinessBaseSerializer):
    """
    Serializer specifically for updating business details
    """
    class Meta(BusinessBaseSerializer.Meta):
        # model = Business
        fields = [
            'name',
            'description',
            'address_one',
            'address_two',
            'phone',
            'email',
            'registration_number',
            'logo_url',
            'brand_color_one',
            'brand_color_two',
            'currency',
            'tax_rate',
            'tax_enabled',
            'selected_template_id',
            'motto',
            'signature_type',
            'signature_text',
            'signature_url',
            'onboarding_complete',
        ]
        
    def validate(self, attrs):
        # Ensure tax rate is valid if tax is enabled
        tax_enabled = attrs.get('tax_enabled', self.instance.tax_enabled if self.instance else False)
        if tax_enabled:
            tax_rate = attrs.get('tax_rate', self.instance.tax_rate if self.instance else 0)
            if tax_rate <= 0:
                raise serializers.ValidationError({'tax_rate': 'Tax rate must be > 0 when tax is enabled'})
        
        return self.validate_signatures(attrs)

    def update(self, instance, validated_data):
        sync_trigger_fields = [
            'name', 'address_one', 'address_two', 'phone', 'email',
            'registration_number', 'tax_rate', 'tax_enabled', 'currency','brand_color_one', 'brand_color_two',
        ]
        needs_sync = any(
            field in validated_data and validated_data[field] != getattr(instance, field)
            for field in sync_trigger_fields
        )
        if needs_sync and instance.sync_status == SyncStatus.SYNCED:
            validated_data['sync_status'] = SyncStatus.PENDING
        return super().update(instance, validated_data)


class BusinessSyncSerializer(serializers.ModelSerializer):
    """
    Serializer for sync-related operations
    """
    class Meta:
        model = Business
        fields = ['id', 'server_id', 'sync_status', 'updated_at']
        read_only_fields = ['id', 'updated_at']
    
    def validate_sync_status(self, value):
        """Validate sync status transitions"""
        if self.instance:
            current_status = self.instance.sync_status
            
            valid_transitions = {
                SyncStatus.PENDING: [SyncStatus.SYNCED, SyncStatus.ERROR],
                SyncStatus.SYNCED: [SyncStatus.PENDING, SyncStatus.ERROR, SyncStatus.DELETED],
                SyncStatus.ERROR: [SyncStatus.PENDING, SyncStatus.SYNCED],
                SyncStatus.DELETED: [],
            }
            
            if value not in valid_transitions.get(current_status, []):
                raise serializers.ValidationError(
                    f"Cannot transition from {current_status} to {value}"
                )
        
        return value
    
    def validate(self, attrs):
        """Ensure server_id is set when status is SYNCED"""
        sync_status = attrs.get('sync_status', self.instance.sync_status if self.instance else None)
        server_id = attrs.get('server_id', self.instance.server_id if self.instance else None)
        
        if sync_status == SyncStatus.SYNCED and not server_id:
            raise serializers.ValidationError({
                'server_id': 'Server ID is required when sync status is SYNCED'
            })
        
        return attrs


class BusinessOnboardingSerializer(serializers.ModelSerializer):
    """
    Serializer for completing onboarding process
    """
    class Meta:
        model = Business
        fields = ['onboarding_complete', 'selected_template_id']
    
    def validate(self, attrs):
        """Ensure required fields are set before completing onboarding"""
        if attrs.get('onboarding_complete', False):
            instance = self.instance
            required_fields = ['name', 'phone', 'email', 'address_one', 'selected_template_id']
            
            missing_fields = []
            for field in required_fields:
                value = attrs.get(field, getattr(instance, field) if instance else None)
                if not value:
                    missing_fields.append(field)
            
            if missing_fields:
                raise serializers.ValidationError({
                    'onboarding_complete': f'Cannot complete onboarding. Missing required fields: {", ".join(missing_fields)}'
                })
        
        return attrs
from rest_framework import serializers
from .models import Business, SyncStatus
from accounts.models import User


class BusinessSerializer(serializers.ModelSerializer):
    owner_email = serializers.ReadOnlyField(source='owner.email')

    class Meta:
        model = Business
        fields = [
            'id', 'owner_email', 'name', 'description', 'address_one', 
            'address_two', 'phone', 'email', 'registration_number', 
            'logo', 'currency', 'tax_rate', 'tax_enabled', 
            'selected_template_id', 'onboarding_complete', 'motto', 'server-id',
            'signature', 'sync_status', 'created_at'
        ]
        read_only_fields = ['id', 'sync_status', 'created_at']
 
    
    def validate_tax_rate(self, value):
        """Ensure tax rate is between 0 and 1 (0% to 100%)"""
        if value < 0 or value > 1:
            raise serializers.ValidationError("Tax rate must be between 0.0000 and 1.0000 (0% to 100%)")
        return value
    
    def validate_phone(self, value):
        """Enhanced phone validation"""
        if not value:
            raise serializers.ValidationError("Phone number is required")
        
        cleaned = value.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        
        if not cleaned.isdigit():
            raise serializers.ValidationError("Phone number must contain only digits and common formatting characters (+, -, spaces, parentheses)")
        
        if len(cleaned) < 7 or len(cleaned) > 15:
            raise serializers.ValidationError("Phone number must be between 7 and 15 digits")
        
        return value
    
    def validate_email(self, value):
        """Validate business email"""
        if not value:
            raise serializers.ValidationError("Email is required")
        return value.lower()
    
    def validate_currency(self, value):
        """Validate currency code format"""
        if not value:
            raise serializers.ValidationError("Currency is required")
        
        if len(value) != 3:
            raise serializers.ValidationError("Currency code must be 3 characters (ISO 4217 format)")
        
        return value.upper()
    
    def validate_logo(self, value):
        """Validate logo URL"""
        if value:
            if not value.startswith(('http://', 'https://')):
                raise serializers.ValidationError("Logo must be a valid URL starting with http:// or https://")
        return value
    
    def validate_signature(self, value):
        """Validate signature field"""
        if value:
            if value.startswith('data:image'):
                if 'base64,' not in value:
                    raise serializers.ValidationError("Invalid base64 signature format")
            elif not value.startswith(('http://', 'https://')):
                raise serializers.ValidationError("Signature must be either a valid URL or base64 data URI")
        return value
    
    def validate(self, attrs):
        """Cross-field validation"""
        if attrs.get('tax_enabled', False):
            tax_rate = attrs.get('tax_rate', 0)
            if tax_rate <= 0:
                raise serializers.ValidationError({
                    'tax_rate': 'Tax rate must be greater than 0 when tax is enabled'
                })
        
        if not self.instance:
            owner = attrs.get('owner')
            if owner and Business.objects.filter(owner=owner).exists():
                raise serializers.ValidationError({
                    'owner': 'This user already has a business registered'
                })
        
        return attrs


class BusinessListSerializer(serializers.ModelSerializer):
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


class BusinessCreateSerializer(serializers.ModelSerializer):
    """
    Serializer specifically for creating a new business
    """
    class Meta:
        model = Business
        fields = [
            'name',
            'description',
            'address_one',
            'address_two',
            'phone',
            'email',
            'registration_number',
            'logo',
            'currency',
            'tax_rate',
            'tax_enabled',
            'selected_template_id',
            'motto',
            'signature',
        ]
        extra_kwargs = {
            'description': {'required': False, 'allow_blank': True},
            'address_two': {'required': False, 'allow_blank': True, 'allow_null': True},
            'registration_number': {'required': False, 'allow_blank': True, 'allow_null': True},
            'logo': {'required': False, 'allow_null': True},
            'motto': {'required': False, 'allow_blank': True, 'allow_null': True},
            'signature': {'required': False, 'allow_blank': True},
        }
    
    def create(self, validated_data):
        """Create business and assign owner from context"""
        owner = self.context['request'].user
        validated_data['owner'] = owner
                    
        return super().create(validated_data)
    
    def validate(self, attrs):
        """Validate business creation"""
        owner = self.context['request'].user
        if Business.objects.filter(owner=owner).exists():
            raise serializers.ValidationError('You already have a business registered')
        
        return attrs


class BusinessUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer specifically for updating business details
    """
    class Meta:
        model = Business
        fields = [
            'name',
            'description',
            'address_one',
            'address_two',
            'phone',
            'email',
            'registration_number',
            'logo',
            'currency',
            'tax_rate',
            'tax_enabled',
            'selected_template_id',
            'onboarding_complete',
            'motto',
            'signature',
        ]
        extra_kwargs = {
            'description': {'required': False, 'allow_blank': True},
            'address_two': {'required': False, 'allow_blank': True, 'allow_null': True},
            'registration_number': {'required': False, 'allow_blank': True, 'allow_null': True},
            'logo': {'required': False, 'allow_null': True},
            'motto': {'required': False, 'allow_blank': True, 'allow_null': True},
            'signature': {'required': False, 'allow_blank': True},
        }
    
    def validate(self, attrs):
        """Cross-field validation for updates"""
        if attrs.get('tax_enabled', False):
            tax_rate = attrs.get('tax_rate', self.instance.tax_rate if self.instance else 0)
            if tax_rate <= 0:
                raise serializers.ValidationError({
                    'tax_rate': 'Tax rate must be greater than 0 when tax is enabled'
                })
        
        return attrs
    
    def update(self, instance, validated_data):
        """Update and mark as needing sync if relevant fields changed"""
        sync_trigger_fields = [
            'name', 'address_one', 'address_two', 'phone', 'email',
            'registration_number', 'tax_rate', 'tax_enabled', 'currency'
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
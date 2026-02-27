from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import User

# --- 1. Base / Detail Serializer ---
class UserSerializer(serializers.ModelSerializer):
    """
    Full serializer for User model profile details.
    Used for retrieving or updating the full user profile.
    """
    has_business = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'email_verified_at',
            'created_at',
            'updated_at',
            'email_notifications',
            'push_notifications',
            'has_business',
            'is_active',
            'is_staff',
        ]
        read_only_fields = [
            'id', 
            'email_verified_at', 
            'created_at', 
            'updated_at', 
            'is_staff', 
            'has_business'
        ]

    def get_has_business(self, obj):
        """Check if user has a related business record"""
        return hasattr(obj, 'business')


# --- 2. Registration Serializer ---
class UserRegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for initial user signup.
    Strictly handles email and password hashing.
    """
    password = serializers.CharField(
        write_only=True, 
        min_length=6, 
        max_length=128,
        style={'input_type': 'password'}
    )

    class Meta:
        model = User
        fields = ['email', 'password']

    def validate_email(self, value):
        """Normalize email and check uniqueness"""
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists")
        return value

    def create(self, validated_data):
        """Uses the Custom UserManager to ensure password hashing"""
        return User.objects.create_user(**validated_data)


# --- 3. Update Serializer ---
class UserUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer focused on user-editable settings.
    """
    class Meta:
        model = User
        fields = [
            'email_notifications',
            'push_notifications',
        ]



# --- 4. Password Management Serializer ---
class ChangePasswordSerializer(serializers.Serializer):
    """
    Specialized serializer for secure password updates.
    """
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True, min_length=6)
    new_password_confirm = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError({"new_password_confirm": "New passwords do not match"})
        
        user = self.context['request'].user
        if not user.check_password(attrs['old_password']):
            raise serializers.ValidationError({"old_password": "Old password is incorrect"})
            
        if attrs['old_password'] == attrs['new_password']:
            raise serializers.ValidationError({"new_password": "New password must be different from old"})
            
        return attrs

    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save()
        return user


# --- 5. List Serializer ---
class UserListSerializer(serializers.ModelSerializer):
    """
    Lightweight version of the User model for dashboard tables.
    """
    full_name = serializers.SerializerMethodField()
    has_business = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'full_name',
            'has_business',
            'is_active',
            'is_staff',
            'created_at',
            'status',
        ]
        read_only_fields = fields

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.email

    def get_status(self, obj):
        if obj.is_staff:
            return "Admin"
        return "User"

    def get_has_business(self, obj):
        return hasattr(obj, 'business')
    


class AdminDashboardSerializer(serializers.Serializer):
    """
    A custom serializer that isn't tied to a specific model.
    Perfect for sending summary stats to Next.js.
    """
    total_users = serializers.IntegerField()
    active_users = serializers.IntegerField()
    verified_users = serializers.IntegerField()
    new_users_today = serializers.IntegerField()  
      
    
class UserWithBusinessSerializer(UserSerializer):
    
    from business.serializers import BusinessSerializer
    business = BusinessSerializer(read_only=True)
    
    
    class Meta(UserSerializer.Meta):
        
        
        # This takes all fields from UserSerializer and adds 'business'
        fields = UserSerializer.Meta.fields + ['business']    
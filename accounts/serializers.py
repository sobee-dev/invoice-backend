# accounts/serializers.py
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from accounts.terms import terms_fields
from accounts.validators import reject_disposable_email

from .google import GoogleAuthError, has_real_password, require_recent, user_info_from_proof
from business.serializers import BusinessSerializer
from business.utils import get_user_business
from .models import AccountStatus, AccountStatusAudit, PasswordResetToken, User


# ============================================
# 1. Base / Detail Serializer
# ============================================
class UserSerializer(serializers.ModelSerializer):
    """
    Full serializer for User model profile details.
    Used for retrieving or updating the full user profile.
    """
    has_business = serializers.SerializerMethodField()
    full_name = serializers.SerializerMethodField()
    has_password = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'first_name',
            'last_name',
            'full_name',
            'role',
            'email_verified_at',
            'email_notifications',
            'push_notifications',
            'requires_password_change',
            'password_changed_at',
            'has_business',
            'has_password',
            'is_active',
            'is_staff',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'email_verified_at',
            'created_at',
            'updated_at',
            'is_staff',
            'has_business',
            'has_password',
            'requires_password_change',
            'password_changed_at',
        ]

    def get_has_business(self, obj):
        return get_user_business(obj) is not None

    def get_has_password(self, obj):
        return has_real_password(obj)
    
    def get_full_name(self, obj):
        """Get full name from first and last name"""
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.email


# ============================================
# 2. Registration Serializer
# ============================================
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
    accepted_terms = serializers.BooleanField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'first_name', 'last_name', 'accepted_terms']

    
    def validate_accepted_terms(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "You must accept the Terms of Service and Privacy Policy."
            )
        return value
    
    def validate_email(self, value):
        """Normalize email and check uniqueness"""
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists")
        return value

    def validate_password(self, value):
        """Validate password strength"""
        try:
            validate_password(value)
        except serializers.ValidationError as e:
            raise serializers.ValidationError(e.messages)
        return value

    def create(self, validated_data):
        """Uses the Custom UserManager to ensure password hashing"""
        validated_data.pop('accepted_terms')
        return User.objects.create_user(**validated_data, **terms_fields())

    def validate_email(self, value):
        value = value.lower()
        reject_disposable_email(value)
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists")
        return value

# ============================================
# 3. Update Serializer
# ============================================
class UserUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer focused on user-editable settings.
    """
    class Meta:
        model = User
        fields = [
            'first_name',
            'last_name',
            'email_notifications',
            'push_notifications',
        ]


# ============================================
# 4. Password Management Serializer
# ============================================
class ChangePasswordSerializer(serializers.Serializer):
    """
    Specialized serializer for secure password updates.
    On first login (requires_password_change=True), old_password is not required.
    """
    old_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={'input_type': 'password'}
    )
    new_password = serializers.CharField(
        write_only=True,
        min_length=6,
        style={'input_type': 'password'}
    )
    new_password_confirm = serializers.CharField(
        write_only=True,
        min_length=6,
        style={'input_type': 'password'}
    )

    def validate(self, attrs):
        user = self.context['request'].user
        
        # If not first-time password change, require old password
        if not user.requires_password_change:
            old_password = attrs.get('old_password', '')
            if not old_password:
                raise serializers.ValidationError(
                    {"old_password": "Old password is required."}
                )
            if not user.check_password(old_password):
                raise serializers.ValidationError(
                    {"old_password": "Old password is incorrect."}
                )
        
        # Check new passwords match
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError(
                {"new_password_confirm": "New passwords do not match."}
            )
        
        # Validate new password strength
        try:
            validate_password(attrs['new_password'], user=user)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"new_password": e.messages})
        
        # Check new password is different from old
        if not user.requires_password_change:
            if attrs.get('old_password') == attrs['new_password']:
                raise serializers.ValidationError(
                    {"new_password": "New password must be different from old password."}
                )
            
        return attrs

    def save(self):
        """Update password and clear requires_password_change flag"""
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.requires_password_change = False
        user.password_changed_at = timezone.now()
        user.save()
        return user


# ============================================
# 5. List Serializer
# ============================================
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
            'role',
            'has_business',
            'is_active',
            'is_staff',
            'created_at',
            'status',
        ]
        read_only_fields = fields

    def get_full_name(self, obj):
        """Get full name from first and last name"""
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.email

    def get_status(self, obj):
        """Get user status"""
        if obj.is_staff:
            return "Admin"
        elif obj.role == 'owner':
            return "Owner"
        elif obj.role == 'staff':
            return "Staff"
        return "User"

    def get_has_business(self, obj):
        """Check if user has a related business record"""
        return hasattr(obj, 'business')


# ============================================
# 6. Login Response Serializer
# ============================================
class LoginResponseSerializer(serializers.Serializer):
    """
    Response from login endpoint.
    Includes tokens and user info with password change flag.
    """
    access = serializers.CharField()
    refresh = serializers.CharField()
    user = serializers.SerializerMethodField()
    requires_password_change = serializers.BooleanField()

    def get_user(self, obj):
        """Format user data in response"""
        user = obj['user']
        return {
            'id': str(user.id),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'role': user.role,
            'full_name': f"{user.first_name} {user.last_name}".strip() or user.email,
        }


# ============================================
# 7. Admin Dashboard Serializer
# ============================================
class AdminDashboardSerializer(serializers.Serializer):
    """
    A custom serializer that isn't tied to a specific model.
    Perfect for sending summary stats.
    """
    total_users = serializers.IntegerField()
    active_users = serializers.IntegerField()
    verified_users = serializers.IntegerField()
    new_users_today = serializers.IntegerField()


# ============================================
# 8. User with Business Serializer
# ============================================
class UserWithBusinessSerializer(UserSerializer):
    """
    Extended user serializer that includes business details.
    Used for owners to see their complete profile.
    """
    from business.serializers import BusinessSerializer
    business = BusinessSerializer(read_only=True)
    
    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ['business']
        
        
    def get_business(self, obj):
        biz = get_user_business(obj)
        # if hasattr(obj, 'business'):
        return BusinessSerializer(biz).data if biz else None

class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    # No validate_email uniqueness check here — deliberately. See view below:
    # this serializer must never reveal whether an email exists.


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=6)
    new_password_confirm = serializers.CharField(write_only=True, min_length=6)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError(
                {"new_password_confirm": "Passwords do not match."}
            )
        try:
            validate_password(attrs['new_password'])
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"new_password": e.messages})

        record = PasswordResetToken.verify(attrs['token'])
        if record is None:
            raise serializers.ValidationError(
                {"token": "This reset link is invalid or has expired."}
            )
        self.token_record = record
        return attrs

    def save(self):
        user = self.token_record.user
        user.set_password(self.validated_data['new_password'])
        user.requires_password_change = False
        user.password_changed_at = timezone.now()
        user.save()

        self.token_record.used_at = timezone.now()
        self.token_record.save(update_fields=['used_at'])

        # Kill any existing sessions — a password reset should log out
        # every device that was logged in with the old password.
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
        for outstanding in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=outstanding)

        return user    
    
class AccountDeletionRequestSerializer(serializers.Serializer):
    """
    Owner or staff initiated deletion. Accounts with a password confirm with
    that password. Google only accounts confirm with a fresh Google sign in:
    google_code from the web flow, or google_id_token from the mobile SDK.
    Errors are lists so existing mobile code that reads field[0] keeps working.
    """
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    google_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    google_id_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate(self, attrs):
        user = self.context['request'].user

        if has_real_password(user):
            password = attrs.get('password', '')
            if not password:
                raise serializers.ValidationError({'password': ['Password is required.']})
            if not user.check_password(password):
                raise serializers.ValidationError({'password': ['Incorrect password.']})
            return attrs

        try:
            info = user_info_from_proof(attrs.get('google_code'), attrs.get('google_id_token'))
            require_recent(info)
        except GoogleAuthError as e:
            raise serializers.ValidationError({'google': [e.message]})

        if info['email'] != user.email.lower():
            raise serializers.ValidationError(
                {'google': ['That Google account does not match this account.']}
            )
        return attrs

    def save(self):
        from .models import AccountStatus
        from .services import set_account_status
        return set_account_status(
            user=self.context['request'].user,
            new_status=AccountStatus.PENDING_DELETION,
            reason=self.validated_data.get('reason', ''),
        )


class CancelAccountDeletionSerializer(serializers.Serializer):
    """
    Self-service reactivation for an account still inside its grace period.
    Deliberately doesn't use Django's authenticate(): ModelBackend refuses
    inactive users outright, and a pending-deletion account is inactive by
    design. Password accounts send email and password. Google accounts send a
    fresh google_code or google_id_token, and the email comes from Google.
    """
    email = serializers.EmailField(required=False)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    google_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    google_id_token = serializers.CharField(write_only=True, required=False, allow_blank=True)

    def _find_user(self, attrs):
        google_code = attrs.get('google_code')
        google_id_token = attrs.get('google_id_token')

        if google_code or google_id_token:
            try:
                info = user_info_from_proof(google_code, google_id_token)
                require_recent(info)
            except GoogleAuthError as e:
                raise serializers.ValidationError({'error': e.message})
            user = User.objects.filter(email=info['email']).first()
            if user is None:
                raise serializers.ValidationError({'error': 'No account was found for that Google account.'})
            return user

        email = (attrs.get('email') or '').lower()
        password = attrs.get('password', '')
        if not email or not password:
            raise serializers.ValidationError({'error': 'Email and password are required.'})

        user = User.objects.filter(email=email).first()
        if user is None or not user.check_password(password):
            raise serializers.ValidationError({'error': 'Invalid email or password.'})
        return user

    def validate(self, attrs):
        user = self._find_user(attrs)

        if user.status != AccountStatus.PENDING_DELETION:
            raise serializers.ValidationError({'error': 'This account is not scheduled for deletion.'})

        if user.deletion_scheduled_for and user.deletion_scheduled_for <= timezone.now():
            raise serializers.ValidationError({'error': 'The deletion grace period has already ended.'})

        self.user = user
        return attrs

    def save(self):
        from .services import set_account_status
        return set_account_status(
            user=self.user, new_status=AccountStatus.ACTIVE,
            reason='Self-service reactivation before grace period ended.',
        )


class AdminAccountStatusSerializer(serializers.Serializer):
    """Used by UserViewSet.set_status — the API the admin dashboard calls."""
    status = serializers.ChoiceField(choices=AccountStatus.choices)
    reason = serializers.CharField(max_length=1000)  # required — no silent status changes


class AccountStatusAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountStatusAudit
        fields = ['id', 'user_id', 'email', 'from_status', 'to_status', 'reason', 'actor_email', 'created_at']
        read_only_fields = fields    
    

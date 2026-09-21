# accounts/models.py
# ============================================
from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
import uuid
import hashlib
import secrets
from datetime import timedelta
from django.utils import timezone

from receipt_backend_api import settings
 
 
class AccountStatus(models.TextChoices):
    ACTIVE = 'active', 'Active'
    SUSPENDED = 'suspended', 'Suspended'
    PENDING_DELETION = 'pending_deletion', 'Pending Deletion'
    DELETED = 'deleted', 'Deleted' 
 
class UserManager(BaseUserManager):
    def _create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The email must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user
 
    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)
 
    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self._create_user(email, password, **extra_fields)
 
 
class User(AbstractUser):
    """
    Custom user model with role-based access and password change tracking.
    
    - Owners: Have a OneToOne Business relationship
    - Staff: Can belong to a business via StaffMember
    - Admins: System administrators
    """
    
    ROLE_CHOICES = [
        ('owner', 'Business Owner'),
        ('staff', 'Staff'),
        # ('admin', 'Admin'),
    ]
 
    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Authentication
    username = None  # Removed for email-only login
    email = models.EmailField(unique=True)
    
    # User info
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    
    # Role and permissions
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='owner'
    )
    
    # Email verification
    email_verified_at = models.DateTimeField(null=True, blank=True)
    
    # Notification preferences
    email_notifications = models.BooleanField(default=True)
    push_notifications = models.BooleanField(default=True)
    
    # Password change on first login (for new staff)
    requires_password_change = models.BooleanField(
        default=False,
        help_text="Staff must change password on first login"
    )
    password_changed_at = models.DateTimeField(null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
     # ── Account lifecycle ──
    status = models.CharField(
        max_length=20, choices=AccountStatus.choices,
        default=AccountStatus.ACTIVE, db_index=True,
    )
    status_reason = models.TextField(blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    deletion_scheduled_for = models.DateTimeField(null=True, blank=True)
    
    # Custom manager
    objects = UserManager()
    
    # Django auth
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []
 
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['role']),
        ]
        
        constraints = [
            models.CheckConstraint(
                check=models.Q(requires_password_change=False) | models.Q(role='staff'),
                name='requires_password_change_only_for_staff',
            ),
        ]
 
    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"
 
    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
 
    @property
    def is_owner(self):
        """Check if user is a business owner"""
        return self.role == 'owner'
 
    @property
    def is_staff_member(self):
        """Check if user is a staff member"""
        return self.role == 'staff'
    
    def save(self, *args, **kwargs):
        if self.requires_password_change and self.role != 'staff':
            raise ValueError(
                "requires_password_change can only be True for staff accounts."
            )
        super().save(*args, **kwargs)
        
class AccountStatusAudit(models.Model):
    """
    Deliberately NOT a ForeignKey to User — user_id is a plain UUIDField so
    this row survives after purge_expired_accounts hard-deletes the account.
    This table is the permanent record of "who changed what status, when,
    and why" — it's what you'll pull up if a deleted user ever disputes it.
    """
    id = models.BigAutoField(primary_key=True)
    user_id = models.UUIDField(db_index=True)
    email = models.EmailField()
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    reason = models.TextField(blank=True)
    actor_id = models.UUIDField(null=True, blank=True)     # null = self-service (owner did it themselves)
    actor_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user_id'])]

    def __str__(self):
        return f"{self.email}: {self.from_status} → {self.to_status} ({self.created_at:%Y-%m-%d})"
    
class PasswordResetToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens',
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    TTL_MINUTES = 30

    @classmethod
    def issue(cls, user):
        # Invalidate any earlier unused tokens for this user first —
        # only one live reset link should ever work at a time.
        cls.objects.filter(user=user, used_at__isnull=True).delete()

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        cls.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=timezone.now() + timedelta(minutes=cls.TTL_MINUTES),
        )
        return raw_token  # only the raw token is ever emailed; DB only has the hash

    @classmethod
    def verify(cls, raw_token):
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        try:
            record = cls.objects.select_related('user').get(token_hash=token_hash)
        except cls.DoesNotExist:
            return None
        if record.used_at is not None:
            return None
        if record.expires_at < timezone.now():
            return None
        return record    
    

    
class AccountDeletionLog(models.Model):
    user_id = models.UUIDField()          # not a FK — must survive user deletion
    email = models.EmailField()           # snapshot at time of request
    role = models.CharField(max_length=20)
    requested_at = models.DateTimeField()
    executed_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
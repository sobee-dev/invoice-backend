import uuid
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


class SyncStatus(models.TextChoices):
    """Sync status choices"""
    PENDING = 'pending', 'Pending'
    SYNCED = 'synced', 'Synced'
    ERROR = 'error', 'Error'
    DELETED = 'deleted', 'Deleted'



class Business(models.Model):
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
# Link the Business to a User (The Owner)
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE,
        related_name='business'
    )
        
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    address_one = models.CharField(max_length=255)
    address_two = models.CharField(max_length=255, null=True, blank=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField()
    registration_number = models.CharField(max_length=100, null=True, blank=True)
    
    # Store URL for the logo
    logo_url = models.URLField(null=True, blank=True)
    
    currency = models.CharField(max_length=10, default="USD")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    tax_enabled = models.BooleanField(default=False)
    
    selected_template_id = models.CharField(max_length=100)
    onboarding_complete = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    motto = models.CharField(max_length=255, null=True, blank=True)
    signature_type = models.CharField(
        choices=[("none","none"),("text","text"),("image","image")]
    )
    brand_color_one = models.CharField(max_length=8, default="#ca3a5e")
    brand_color_two = models.CharField(max_length=8, default="#A7A3A3")
    signature_text = models.CharField(max_length=255, blank=True, null=True)
    signature_url = models.URLField(blank=True, null=True)
    
    server_id = models.IntegerField(null=True, blank=True)
    
    sync_status = models.CharField(
        max_length=20,
        choices=SyncStatus.choices,
        default=SyncStatus.PENDING)
    
    def clean(self):
        # Move ALL core logic here
        if self.signature_type == 'none':
            self.signature_text = ''
            self.signature_url = ''
        
        if self.signature_type == 'text' and not self.signature_text:
            raise ValidationError({'signature_text': 'Text signature is required'})

        if self.signature_type == 'image' and not self.signature_url:
            raise ValidationError({'signature_url': 'Signature image is required'})

    def save(self, *args, **kwargs):
        self.full_clean() # Triggers clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
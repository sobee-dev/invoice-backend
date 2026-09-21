from django.db import models

import uuid

from django.conf import settings
from django.db import models


class PushToken(models.Model):
    """
    One row per device push token. A user can have several active
    tokens at once (multiple devices signed into the same account) —
    the unique constraint lives on the token itself, not per-user,
    since an Expo push token identifies a single device+app install
    and should only ever belong to whoever is currently logged in on
    that device.
    """

    PLATFORM_CHOICES = [
        ('ios', 'iOS'),
        ('android', 'Android'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='push_tokens',
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return f"{self.user.email} — {self.platform or 'unknown'} ({self.token[:24]}…)"


class Notification(models.Model):
    """
    Persisted notification record — the source of truth for the in-app
    notification center. A push (Expo) is a best-effort side channel
    on top of this; if the push fails to send or fails to deliver, the
    notification still exists here and still shows up in-app.

    Every notification type from the planning doc maps to a value in
    TYPE_CHOICES. Add new types here as new triggers are built —
    nothing else in the dispatch layer needs to change.
    """

    TYPE_CHOICES = [
        # Tier 1 — event-driven
        ('low_stock', 'Low Stock Alert'),
        ('out_of_stock', 'Out of Stock'),
        ('payment_due', 'Payment Due'),
        ('invoice_overdue', 'Invoice Overdue'),
        ('unpaid_invoices', 'Unpaid Invoices'),
        # Tier 2 — scheduled
        ('daily_sales_summary', 'Daily Sales Summary'),
        ('sales_milestone', 'Sales Milestone'),
        ('profit_performance', 'Profit/Performance Update'),
        ('sales_drop', 'Unusual Sales Drop'),
        ('weekly_report', 'Weekly Business Report'),
        # Tier 3 — product/customer insight
        ('best_selling_product', 'Best-Selling Product'),
        ('fast_moving_stock', 'Fast-Moving Stock'),
        ('restock_reminder', 'Inventory Restock Reminder'),
        ('stock_adjustment', 'Stock Adjustment'),
        ('new_customer', 'New Customer'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    business = models.ForeignKey(
        'business.Business',
        on_delete=models.CASCADE,
        related_name='notifications',
        help_text="Every notification belongs to a business, not a user directly — "
                  "owner and staff on the same business see the same feed.",
    )

    notification_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    title = models.CharField(max_length=150)
    body = models.CharField(max_length=500)

    # Structured payload for deep-linking from a notification tap —
    # e.g. {"product_id": "...", "dedup_key": "..."}. dedup_key is
    # reserved by the dispatch layer's throttle mechanism; anything
    # else here is free for the frontend to use.
    data = models.JSONField(default=dict, blank=True)

    push_sent = models.BooleanField(
        default=False,
        help_text="Whether Expo accepted the push. False means only the push side "
                  "channel failed — the notification itself is still valid.",
    )
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'read_at']),
            models.Index(fields=['business', 'created_at']),
            models.Index(fields=['notification_type']),
        ]

    def __str__(self):
        return f"[{self.notification_type}] {self.title} — {self.business.name}"

    @property
    def is_read(self):
        return self.read_at is not None
    
    
    
    
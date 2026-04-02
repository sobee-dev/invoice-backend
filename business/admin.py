from django.contrib import admin
from django.db.models import Count, Sum
from django.utils.html import format_html
from .models import Business


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):

    # ── Custom computed columns ──────────────────────────────────────────────

    def receipt_count(self, obj):
        return obj.receipt_count
    receipt_count.short_description = "Receipts Issued"
    receipt_count.admin_order_field = "receipt_count"  # makes column sortable

    def total_revenue(self, obj):
        total = obj.total_revenue or 0
        return f"{obj.currency}{total:,.2f}"
    total_revenue.short_description = "Total Revenue"
    total_revenue.admin_order_field = "total_revenue"

    def logo_preview(self, obj):
        if obj.logo_url:
            return format_html(
                '<img src="{}" style="height:40px;border-radius:4px;" />',
                obj.logo_url
            )
        return "—"
    logo_preview.short_description = "Logo"

    # ── List view ────────────────────────────────────────────────────────────

    list_display = [
        "name", "owner", "email", "phone",
        "receipt_count", "total_revenue",
        "onboarding_complete", "created_at",
    ]
    list_filter = ["onboarding_complete", "tax_enabled", "created_at", "currency"]
    search_fields = ["name", "owner__email", "email", "phone"]
    ordering = ["-created_at"]
    raw_id_fields = ["owner"]
    readonly_fields = ["created_at", "updated_at", "logo_preview", "receipt_count", "total_revenue"]

    # ── Detail/edit page ─────────────────────────────────────────────────────

    fieldsets = (
        ("Business Info", {"fields": (
            "owner", "name", "description",
            "email", "phone",
            "address_one", "address_two",
            "registration_number",
        )}),
        ("Branding", {"fields": (
            "logo_url", "logo_preview",
            "brand_color_one", "brand_color_two",
            "motto", "selected_template_id",
        )}),
        ("Signature", {"fields": (
            "signature_type", "signature_text", "signature_url",
        )}),
        ("Tax & Currency", {"fields": (
            "currency", "tax_rate", "tax_enabled",
        )}),
        ("Stats", {"fields": (
            "receipt_count", "total_revenue",
        )}),
        ("Status", {"fields": (
            "onboarding_complete", "sync_status", "server_id",
        )}),
        ("Timestamps", {"fields": (
            "created_at", "updated_at",
        )}),
    )

    # ── Annotate queryset so receipt_count and total_revenue are sortable ────

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            receipt_count=Count("receipts"),
            total_revenue=Sum("receipts__grand_total"),
        )
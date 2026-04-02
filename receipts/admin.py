from django.contrib import admin
from .models import Receipt

@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    
    list_display = [
        "receipt_number", "business", "customer_name",
        "grand_total",  "created_at"
    ]
    list_filter = ["created_at", "business"]
    search_fields = [
        "receipt_number", "customer_name",
        "business__name"
    ]
    ordering = ["-created_at"]
    readonly_fields = ["receipt_number", "created_at", "updated_at"]

    fieldsets = (
        ("Receipt Info", {"fields": (
            "receipt_number", "business", "status"
        )}),
        ("Customer", {"fields": (
            "customer_name", "customer_email", "customer_phone"
        )}),
        ("Amounts", {"fields": (
            "subtotal", "tax_amount", "total_amount"
        )}),
        ("Timestamps", {"fields": (
            "created_at", "updated_at"
        )}),
    )

    # Show receipt count per business in the list
    def get_queryset(self, request):
        return super().get_queryset(request).select_related("business")
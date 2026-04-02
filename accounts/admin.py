from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    
    def get_business(self, obj):
        from business.models import Business
        biz = Business.objects.filter(owner=obj).first()
        return biz.name if biz else "—"

    get_business.short_description = "Business"

    list_display = ("email", "first_name", "last_name", "get_business", "is_staff", "is_active", "created_at")

    list_filter = ("is_staff", "is_active", "created_at")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-created_at",)

    fieldsets = (
        ("Account", {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "default_currency")}),
        ("Permissions", {"fields": (
            "is_active", "is_staff", "is_superuser",
            "groups", "user_permissions"
        )}),
        ("Important Dates", {"fields": ("email_verified_at", "last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "is_staff", "is_active"),
        }),
    )

    readonly_fields = ("last_login", "date_joined", "email_verified_at", "created_at")

    # Required since username field is removed
    filter_horizontal = ("groups", "user_permissions")
    
    
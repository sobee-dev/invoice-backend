from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import AccountStatus, AccountStatusAudit, User
from django.shortcuts import render

from django import forms

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from .services import set_account_status


class StatusChangeForm(forms.Form):
    _selected_action = forms.CharField(widget=forms.MultipleHiddenInput)
    reason = forms.CharField(
        widget=forms.Textarea,
        required=True,
        label="Reason — required, recorded in the audit log",
    )


def _make_status_action(new_status: str, label: str):
    def action(modeladmin, request, queryset):
        if 'apply' in request.POST:
            form = StatusChangeForm(request.POST)
            if form.is_valid():
                reason = form.cleaned_data['reason']
                for user in queryset:
                    set_account_status(user=user, new_status=new_status, reason=reason, actor=request.user)
                modeladmin.message_user(request, f"{queryset.count()} account(s) set to {label}.")
                return None
        else:
            form = StatusChangeForm(
                initial={'_selected_action': request.POST.getlist(ACTION_CHECKBOX_NAME)}
            )

        return render(request, 'admin/accounts/status_change_confirm.html', {
            'form': form,
            'accounts': queryset,
            'label': label,
            'action_name': action.__name__,
            'opts': modeladmin.model._meta,
        })

    action.__name__ = f'set_status_{new_status}'
    action.short_description = f"Set status → {label} (reason required)"
    return action


@admin.register(User)
class CustomUserAdmin(UserAdmin):

    def get_business(self, obj):
        from business.models import Business
        biz = Business.objects.filter(owner=obj).first()
        return biz.name if biz else "—"

    get_business.short_description = "Business"

    list_display = (
        "email", "first_name", "last_name", "get_business",
        "status", "is_staff", "is_active", "deletion_scheduled_for", "created_at",
    )

    list_filter = ("status", "is_staff", "is_active", "created_at")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-created_at",)

    fieldsets = (
        ("Account", {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name")}),
        ("Permissions", {"fields": (
            "is_active", "is_staff", "is_superuser",
            "groups", "user_permissions"
        )}),
        ("Account Lifecycle", {"fields": (
            "status", "status_reason", "status_changed_at", "deletion_scheduled_for",
        )}),
        ("Important Dates", {"fields": ("email_verified_at", "last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "is_staff", "is_active"),
        }),
    )

    # status/status_reason are deliberately NOT listed here — they stay
    # editable directly on the change form as an emergency escape hatch
    # (e.g. fixing a bad state by hand), but the normal path for changing
    # them is the actions below, which go through set_account_status()
    # and always write an AccountStatusAudit row. Editing the field
    # directly on the change form does NOT write an audit row — know
    # that trade-off going in.
    readonly_fields = ("last_login", "date_joined", "email_verified_at", "created_at", "status_changed_at", "deletion_scheduled_for")

    filter_horizontal = ("groups", "user_permissions")

    actions = [
        _make_status_action(AccountStatus.SUSPENDED, 'Suspended'),
        _make_status_action(AccountStatus.PENDING_DELETION, 'Pending Deletion'),
        _make_status_action(AccountStatus.ACTIVE, 'Active (Restored)'),
    ]


@admin.register(AccountStatusAudit)
class AccountStatusAuditAdmin(admin.ModelAdmin):
    """Read-only — this table is the audit trail; nobody edits history."""
    list_display = ['email', 'from_status', 'to_status', 'actor_email', 'created_at']
    list_filter = ['from_status', 'to_status']
    search_fields = ['email', 'actor_email', 'reason']
    readonly_fields = [f.name for f in AccountStatusAudit._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
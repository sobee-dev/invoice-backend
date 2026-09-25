# billing/admin.py
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from .models import Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ['business_name', 'plan_tier', 'status', 'paystack_flag',
                     'trial_ends_at', 'current_period_end', 'cancel_at_period_end', 'updated_at']
    list_editable = ['plan_tier', 'status']
    list_filter = ['status', 'plan_tier', 'cancel_at_period_end']
    search_fields = ['business__name', 'business__owner__email', 'paystack_customer_code', 'paystack_subscription_code']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['business']

    fieldsets = (
        (None, {'fields': ('business', 'plan_tier', 'status')}),
        ('Paystack (live — edits here do NOT sync to Paystack, and the next webhook will overwrite this section)', {
            'fields': ('paystack_customer_code', 'paystack_subscription_code', 'paystack_email_token', 'plan_code'),
        }),
        ('Dates', {'fields': ('trial_ends_at', 'current_period_end', 'cancel_at_period_end')}),
        ('Meta', {'fields': ('id', 'created_at', 'updated_at')}),
    )

    actions = ['make_trialing', 'make_active', 'make_grace_period', 'make_canceled']

    @admin.display(description='Business')
    def business_name(self, obj):
        return obj.business.name

    @admin.display(description='Paystack')
    def paystack_flag(self, obj):
        if obj.paystack_subscription_code:
            return format_html('<span style="color:#a05f00;font-weight:600;">⚠ live</span>')
        return format_html('<span style="color:#999;">— none —</span>')

    @admin.action(description='🆕 Set to Trialing (15 days from now)')
    def make_trialing(self, request, queryset):
        count = 0
        for sub in queryset:
            sub.status = Subscription.Status.TRIALING
            sub.trial_ends_at = timezone.now() + timezone.timedelta(days=15)
            sub.save(update_fields=['status', 'trial_ends_at', 'updated_at'])
            count += 1
        self.message_user(request, f'{count} subscription(s) reset to a fresh 15-day trial.')

    @admin.action(description='✅ Set to Active')
    def make_active(self, request, queryset):
        count = queryset.update(status=Subscription.Status.ACTIVE)
        self.message_user(request, f'{count} subscription(s) set to Active.')

    @admin.action(description='⏳ Set to Grace Period (past due, 3 days left)')
    def make_grace_period(self, request, queryset):
        count = 0
        for sub in queryset:
            sub.status = Subscription.Status.PAST_DUE
            sub.current_period_end = timezone.now()
            sub.save(update_fields=['status', 'current_period_end', 'updated_at'])
            count += 1
        self.message_user(request, f'{count} subscription(s) set to Grace Period (locks in ~3 days).')
        
        
    @admin.action(description='🚫 Set to Canceled')
    def make_canceled(self, request, queryset):
        count = queryset.update(status=Subscription.Status.CANCELED)
        self.message_user(request, f'{count} subscription(s) set to Canceled.')
    

    # @admin.action(description='🔒 Set to Expired (locked immediately)')
    # def make_expired(self, request, queryset):
    #     count = queryset.update(status=Subscription.Status.CANCELED)
    #     self.message_user(request, f'{count} subscription(s) set to Expired.')
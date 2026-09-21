from django.contrib import admin


from .models import PushToken


@admin.register(PushToken)
class PushTokenAdmin(admin.ModelAdmin):
    list_display = ('user', 'platform', 'token', 'created_at', 'last_seen_at')
    search_fields = ('user__email', 'token')
    list_filter = ('platform',)

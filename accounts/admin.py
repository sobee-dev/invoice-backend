from django.contrib import admin

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User

class CustomUserAdmin(UserAdmin):
    # This determines which columns show up in the admin list view
    list_display = ('email', 'is_staff', 'is_active', 'created_at')
    
    # This tells the admin to use the email as the main identifier
    ordering = ('email',)
    
    # This is required because we removed the 'username' field
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'default_currency')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('email_verified_at', 'last_login', 'date_joined')}),
    )
    
    # Required for creating users in the admin
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password'),
        }),
    )

admin.site.register(User, CustomUserAdmin)
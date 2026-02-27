from django.contrib import admin
from .models import Business # Import your model

@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
   
    list_display = ('name', 'brand_color_one', 'brand_color_two', 'onboarding_complete')
    def get_user_email(self, obj):
        return obj.user.email if obj.user else "No User Linked"
    
    get_user_email.short_description = 'Linked User'
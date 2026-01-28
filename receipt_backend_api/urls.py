
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
   path('admin/', admin.site.urls), 
   path('api-auth/', include('rest_framework.urls')),
    # API endpoints
   path('api/users/', include('accounts.urls')),
   path('api/business/', include('business.urls')),
   path('api/receipts/', include('receipts.urls')),
    
]

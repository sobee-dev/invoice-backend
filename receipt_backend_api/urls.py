from django.contrib import admin
from django.urls import include, path
from dj_rest_auth.registration.views import SocialLoginView
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter

from .views import health_check

class GoogleLogin(SocialLoginView):
    adapter_class = GoogleOAuth2Adapter

urlpatterns = [
   path('jagaban/', admin.site.urls), 
   path('api-auth/', include('rest_framework.urls')),
   path('health/', health_check, name='health_check'),
   
    # API endpoints
   path('api/auth/google/', GoogleLogin.as_view(), name='google_login'),

   path('api/users/', include('accounts.urls')),
   path('api/business/', include('business.urls')),
   path('api/products/', include('products.urls')),
   path('api/customers/', include('customers.urls')),
   path('api/documents/', include('documents.urls')),
   path('api/inventory/', include('inventory.urls')),
   path('api/staff/', include('staff.urls')),
   path('api/reports/', include('reports.urls')),
   path('api/notifications/', include('notifications.urls')),
   path('api/billing/', include('billing.urls')),
   path('billing/', include('billing.web_urls')),
]
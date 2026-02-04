
from django.contrib import admin
from django.urls import include, path
from dj_rest_auth.registration.views import SocialLoginView
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter

class GoogleLogin(SocialLoginView):
    adapter_class = GoogleOAuth2Adapter

urlpatterns = [
   path('admin/', admin.site.urls), 
   path('api-auth/', include('rest_framework.urls')),
    # API endpoints
   path('api/auth/google/', GoogleLogin.as_view(), name='google_login'),
   path('api/users/', include('accounts.urls')),
   path('api/business/', include('business.urls')),
   path('api/receipts/', include('receipts.urls')),
    
]

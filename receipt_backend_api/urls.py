from django.contrib import admin
from django.urls import include, path
from dj_rest_auth.registration.views import SocialLoginView
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from accounts.views import GoogleCallbackView
from .views import health_check

class GoogleLogin(SocialLoginView):
    adapter_class = GoogleOAuth2Adapter

urlpatterns = [
   path('admin/', admin.site.urls), 
   path('api-auth/', include('rest_framework.urls')),
   path('health/', health_check, name='health_check'),
   
    # API endpoints
   path('api/auth/google/', GoogleLogin.as_view(), name='google_login'),
   path('api/auth/google/callback/', GoogleCallbackView.as_view(), name='google_callback'),  # ← add this
   path('api/users/', include('accounts.urls')),
   path('api/business/', include('business.urls')),
   path('api/receipts/', include('receipts.urls')),
]
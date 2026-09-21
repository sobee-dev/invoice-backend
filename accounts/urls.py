from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PasswordResetPageView, UserViewSet, SessionLimitedTokenRefreshView, oauth_mobile_bridge

router = DefaultRouter()
router.register(r'', UserViewSet, basename='user')

urlpatterns = [
    path('', include(router.urls)),
    path('token/refresh/', SessionLimitedTokenRefreshView.as_view(), name='token_refresh'),
    
    path("oauth/mobile-callback/", oauth_mobile_bridge, name="oauth-mobile-bridge"),
    
    path('reset-password/', PasswordResetPageView.as_view(), name='password-reset-page'),
]
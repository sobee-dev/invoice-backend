from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import GoogleIdTokenLoginView, PasswordResetPageView, UserViewSet, SessionLimitedTokenRefreshView

router = DefaultRouter()
router.register(r'', UserViewSet, basename='user')

urlpatterns = [
    path('token/refresh/', SessionLimitedTokenRefreshView.as_view(), name='token_refresh'),
    path('reset-password/', PasswordResetPageView.as_view(), name='password-reset-page'),
    path('google_login/', GoogleIdTokenLoginView.as_view(), name='google-login'),

    path('', include(router.urls)),   # ← comes last, so it only catches what nothing else matched
]
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import NotificationViewSet, PushTokenViewSet

router = DefaultRouter()
router.register(r'', NotificationViewSet, basename='notification')
router.register(r'push', PushTokenViewSet, basename='push-token')

urlpatterns = [
    path('', include(router.urls)),
]
# notifications/views.py
from django.core.cache import cache
from django.shortcuts import render
from django.utils import timezone

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from business.utils import get_user_business
from notifications.cache_keys import unread_count_cache_key

from .models import Notification
from .serializers import RegisterTokenSerializer, UnregisterTokenSerializer, NotificationSerializer


UNREAD_COUNT_CACHE_TTL = 40  # seconds, kept just under the frontend's poll interval




class PushTokenViewSet(viewsets.ViewSet):
    """
    Not a ModelViewSet — there's no list/retrieve use case for push
    tokens from the client side (a device only ever registers or
    unregisters its own token). Same "custom action" shape used in
    staff/views.py for the same reason.
    """
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['post'])
    def register(self, request):
        """
        POST /api/push/register/
        Body: { "expoPushToken": "...", "platform": "ios" | "android" }

        Call this after login, and again any time Expo issues a fresh
        token (it does happen occasionally). update_or_create in the
        serializer makes repeat calls with the same token harmless.
        """
        serializer = RegisterTokenSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        token_obj = serializer.save()
        return Response(
            {'message': 'Push token registered.', 'id': str(token_obj.id)},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['post'])
    def unregister(self, request):
        """
        POST /api/push/unregister/
        Body: { "expoPushToken": "..." }

        Call this on logout so a signed-out device stops receiving
        pushes meant for the account it just left. Doesn't error if
        the token isn't found — logout shouldn't fail over push
        bookkeeping.
        """
        serializer = UnregisterTokenSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response(
                {'message': 'Token not registered; nothing to do.'},
                status=status.HTTP_200_OK,
            )
        serializer.save()
        return Response({'message': 'Push token unregistered.'}, status=status.HTTP_200_OK)


class NotificationViewSet(viewsets.ViewSet):
    """
    In-app notification center — owner-only by design. Notifications
    carry business-sensitive detail (sales figures, milestones,
    inventory levels) that staff shouldn't see even though
    get_user_business() would technically resolve a business for them
    too. Every action here checks role explicitly rather than relying
    on business scoping alone — same pattern as the owner-only checks
    in staff/views.py (create_staff, invite, deactivate, etc.).

    unread_count is cached per-business for UNREAD_COUNT_CACHE_TTL
    seconds — with several staff/devices on the same business polling
    on roughly the same interval, this collapses what would be N
    identical COUNT queries per window into one. Anything that changes
    read state (mark_read, mark_all_read) or creates a new notification
    (the central notify() dispatch layer — see decisions-and-learnings)
    must invalidate the key via unread_count_cache_key(business.id) so
    a change is never masked by a stale cache entry.
    """
    permission_classes = [permissions.IsAuthenticated]

    def _require_owner(self, request):
        if request.user.role != 'owner':
            return Response(
                {'error': 'Only business owners can view notifications.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def list(self, request):
        """
        GET /api/notifications/
        Optional query params:
          ?unread=true       — only unread notifications
          ?type=low_stock    — filter by notification_type
        """
        denied = self._require_owner(request)
        if denied:
            return denied

        business = get_user_business(request.user)
        if business is None:
            return Response([], status=status.HTTP_200_OK)

        qs = Notification.objects.filter(business=business)

        if request.query_params.get('unread') == 'true':
            qs = qs.filter(read_at__isnull=True)

        notif_type = request.query_params.get('type')
        if notif_type:
            qs = qs.filter(notification_type=notif_type)

        qs = qs[:100]  # model's default ordering is already -created_at

        return Response(NotificationSerializer(qs, many=True).data)

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """
        GET /api/notifications/unread_count/ — for a badge indicator.

        Cached per business for UNREAD_COUNT_CACHE_TTL seconds so a
        poll from a business with several devices open doesn't turn
        into a duplicate query per device on every tick.
        """
        denied = self._require_owner(request)
        if denied:
            return denied

        business = get_user_business(request.user)
        if business is None:
            return Response({'count': 0})

        key = unread_count_cache_key(business.id)
        count = cache.get(key)
        if count is None:
            count = Notification.objects.filter(business=business, read_at__isnull=True).count()
            cache.set(key, count, UNREAD_COUNT_CACHE_TTL)

        return Response({'count': count})

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """POST /api/notifications/{id}/mark_read/"""
        denied = self._require_owner(request)
        if denied:
            return denied

        business = get_user_business(request.user)
        try:
            notification = Notification.objects.get(id=pk, business=business)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found.'}, status=status.HTTP_404_NOT_FOUND)

        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=['read_at'])
            cache.delete(unread_count_cache_key(business.id))

        return Response(NotificationSerializer(notification).data)

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """POST /api/notifications/mark_all_read/"""
        denied = self._require_owner(request)
        if denied:
            return denied

        business = get_user_business(request.user)
        if business is None:
            return Response({'updated': 0})
        updated = Notification.objects.filter(
            business=business, read_at__isnull=True
        ).update(read_at=timezone.now())
        cache.delete(unread_count_cache_key(business.id))
        return Response({'updated': updated})
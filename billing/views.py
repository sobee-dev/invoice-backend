# billing/views.py
from django.conf import settings
from django.http import HttpResponse
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from business.utils import get_user_business
from .models import Subscription
from .serializers import SubscriptionStatusSerializer
from .services import (
    handle_paystack_event, verify_webhook_signature,
    create_subscription_checkout, get_subscription_manage_link, PaystackError,
)
from .permissions import IsBusinessOwner


class SubscriptionStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response({'error': 'No business associated with this account.'}, status=400)
        sub, _ = Subscription.objects.get_or_create(business=business)
        return Response(SubscriptionStatusSerializer(sub).data)


class BillingCheckoutView(APIView):
    """POST /api/billing/checkout/  Body: {"tier": "basic" | "pro"}"""
    permission_classes = [permissions.IsAuthenticated, IsBusinessOwner]

    def post(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response({'error': 'No business associated with this account.'}, status=400)

        tier = request.data.get('tier')
        plan_code = settings.PAYSTACK_PLAN_CODES.get(tier)
        if not plan_code:
            return Response({'error': 'Unknown plan.'}, status=400)

        try:
            url = create_subscription_checkout(
                business=business, plan_code=plan_code,
                callback_url='https://billing.billbuzz.app/success/',
            )
        except PaystackError as e:
            return Response({'error': str(e)}, status=502)
        return Response({'url': url})


class BillingPortalView(APIView):
    """POST /api/billing/portal/ — only valid once a subscription exists."""
    permission_classes = [permissions.IsAuthenticated, IsBusinessOwner]

    def post(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response({'error': 'No business associated with this account.'}, status=400)
        try:
            url = get_subscription_manage_link(business)
        except PaystackError as e:
            return Response({'error': str(e)}, status=400)
        return Response({'url': url})


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def paystack_webhook(request):
    signature = request.META.get('HTTP_X_PAYSTACK_SIGNATURE')
    if not verify_webhook_signature(request.body, signature):
        return HttpResponse(status=400)
    handle_paystack_event(request.data)
    return HttpResponse(status=200)
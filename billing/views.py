# billing/views.py
import logging

from django.conf import settings
from django.http import HttpResponse
from rest_framework import permissions
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import BusinessActivePermission
from business.utils import get_user_business
from .permissions import IsBusinessOwner
from .plans import (
    BILLING_INTERVALS, BUSINESS, CHECKOUT_METHODS, ENTERPRISE, FREE, INTERVAL_DISCOUNT_PERCENT, INTERVAL_LABELS,
    INTERVAL_MONTHS, METHOD_CARD, MONTHLY, ONE_OFF_DAYS, PAID_TIERS, PLAN_FEATURES, PLAN_LIMITS, PLAN_NAMES,
    TRIAL_DAYS, build_comparison, discount_label, get_price_ngn, has_live_card_subscription, is_checkout_pending,
    monthly_equivalent_label, price_label, saving_label, starting_price_label,
)
from .serializers import SubscriptionStatusSerializer
from .services import (
    BillingError, InvalidWebhook, ProviderUnavailable, confirm_checkout, create_checkout, ensure_subscription,
    get_provider, get_subscription_manage_link, process_webhook,
)

logger = logging.getLogger(__name__)

_NO_BUSINESS = {'error': 'No business associated with this account.'}
_PROVIDER_DOWN = {'error': 'Billing is unavailable right now. Try again shortly.', 'code': 'billing_unavailable'}


class SubscriptionStatusView(APIView):
    """GET /api/billing/status/  plan, billing period, limits, usage and trial countdown."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response(_NO_BUSINESS, status=400)
        sub = ensure_subscription(business)
        return Response(SubscriptionStatusSerializer(sub, context={'business': business}).data)


class PlansView(APIView):
    """
    GET /api/billing/plans/  what the plans page shows, on web and mobile.
    Prices arrive as ready-made label strings (price_label, monthly_equivalent_label,
    saving_label, discount_label) so the apps never format or hardcode an amount.
    Each paid plan lists its billing periods, whether card checkout is set up for
    each, and how many days a transfer or USSD purchase buys. `comparison` is the
    plain feature table, built from the same limits the backend enforces.
    Values come from billing/plans.py.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        provider = get_provider()
        plans = []
        for tier in (FREE, BUSINESS, ENTERPRISE):
            intervals = []
            if tier in PAID_TIERS:
                monthly_total = get_price_ngn(tier, MONTHLY)
                for interval in BILLING_INTERVALS:
                    price = get_price_ngn(tier, interval)
                    months = INTERVAL_MONTHS[interval]
                    intervals.append({
                        'interval': interval,
                        'label': INTERVAL_LABELS[interval],
                        'months': months,
                        'price_label': price_label(tier, interval),
                        'monthly_equivalent_label': monthly_equivalent_label(tier, interval),
                        'saving_label': saving_label(tier, interval),
                        'discount_label': discount_label(interval),
                        'discount_percent': INTERVAL_DISCOUNT_PERCENT[interval],
                        # Numbers stay for apps that already read them. New code should use the labels.
                        'price_ngn': price,
                        'monthly_equivalent_ngn': round(price / months),
                        'saving_ngn': monthly_total * months - price,
                        'checkout_available': bool(provider.plan_code_for(tier, interval)),
                        'one_off_available': True,
                        'one_off_days': ONE_OFF_DAYS[interval],
                    })
            plans.append({
                'tier': tier,
                'name': PLAN_NAMES[tier],
                'limits': PLAN_LIMITS[tier],
                'features': PLAN_FEATURES[tier],
                'starting_price_label': starting_price_label(tier) if tier in PAID_TIERS else None,
                'intervals': intervals,
            })
        return Response({
            'plans': plans,
            'comparison': build_comparison(),
            'default_interval': MONTHLY,
            'trial_days': TRIAL_DAYS,
        })


class BillingCheckoutView(APIView):
    """
    POST /api/billing/checkout/  Body: {"tier": "business" | "enterprise",
    "interval": "monthly" | "biannually" | "annually",
    "method": "card" | "transfer"}  (interval defaults to monthly, method to card)
    Returns {"url": ..., "reference": ...}. Redirect the owner to url.

    card is a recurring subscription. transfer is a one-off purchase through bank
    transfer or USSD that buys 30, 180 or 365 days and does not renew by itself.
    Both are refused while a card subscription is active.
    """
    permission_classes = [permissions.IsAuthenticated, BusinessActivePermission, IsBusinessOwner]

    def post(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response(_NO_BUSINESS, status=400)

        tier = request.data.get('tier')
        interval = request.data.get('interval') or MONTHLY
        method = request.data.get('method') or METHOD_CARD
        if tier not in PAID_TIERS:
            return Response({'error': 'Unknown plan.', 'code': 'unknown_plan'}, status=400)
        if interval not in BILLING_INTERVALS:
            return Response({'error': 'Unknown billing period.', 'code': 'unknown_interval'}, status=400)
        if method not in CHECKOUT_METHODS:
            return Response({'error': 'Unknown payment method.', 'code': 'unknown_method'}, status=400)
        if method == METHOD_CARD and not get_provider().plan_code_for(tier, interval):
            return Response(
                {'error': 'That plan is not available for this billing period yet.', 'code': 'plan_unavailable'},
                status=400,
            )

        sub = ensure_subscription(business)

        # A payment is confirmed and waiting for activation. A second checkout
        # now would charge the customer twice.
        if is_checkout_pending(sub):
            return Response(
                {'error': 'We are still confirming your last payment. Check back in a few minutes.',
                 'code': 'checkout_in_progress'},
                status=409,
            )

        if has_live_card_subscription(sub):
            return Response(
                {'error': 'You already have an active subscription. Use the billing portal to manage it.',
                 'code': 'already_subscribed',
                 'plan': sub.plan_tier,
                 'billing_interval': sub.billing_interval or None,
                 'renews_at': sub.current_period_end},
                status=409,
            )

        try:
            result = create_checkout(
                business=business, tier=tier, interval=interval, method=method,
                callback_url=settings.BILLING_SUCCESS_URL,
            )
        except ProviderUnavailable:
            logger.exception('Billing checkout request failed')
            return Response(_PROVIDER_DOWN, status=502)
        except BillingError as e:
            return Response({'error': str(e)}, status=502)
        return Response({'url': result.url, 'reference': result.reference})


class BillingCheckoutConfirmView(APIView):
    """
    POST /api/billing/checkout/confirm/  Body: {"reference": "..."}
    The billing page calls this with the reference the provider put in the return
    URL. It verifies the payment, then returns the same payload as /status/.
    A transfer or USSD payment is applied here and the plan is active in the
    response. A card payment comes back with checkoutPending true until the
    webhook activates it, so keep polling /status/ until plan is business or
    enterprise.
    """
    permission_classes = [permissions.IsAuthenticated, BusinessActivePermission, IsBusinessOwner]

    def post(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response(_NO_BUSINESS, status=400)

        reference = request.data.get('reference')
        try:
            sub = confirm_checkout(business, reference)
        except ValueError as e:
            return Response({'error': str(e), 'code': 'invalid_reference'}, status=400)
        except ProviderUnavailable:
            logger.exception('Billing verify request failed')
            return Response(_PROVIDER_DOWN, status=502)
        except BillingError as e:
            return Response({'error': str(e), 'code': 'payment_not_confirmed'}, status=400)
        return Response(SubscriptionStatusSerializer(sub, context={'business': business}).data)


class BillingPortalView(APIView):
    """POST /api/billing/portal/  only valid for a card subscription that already exists."""
    permission_classes = [permissions.IsAuthenticated, BusinessActivePermission, IsBusinessOwner]

    def post(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response(_NO_BUSINESS, status=400)
        try:
            url = get_subscription_manage_link(business)
        except ProviderUnavailable:
            logger.exception('Billing portal request failed')
            return Response(_PROVIDER_DOWN, status=502)
        except BillingError as e:
            return Response({'error': str(e)}, status=400)
        return Response({'url': url})


@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@throttle_classes([])  # the provider calls from a few IPs, the anonymous rate limit would reject real events
def billing_webhook(request):
    """The signature is verified inside the provider on every call. A bad one gets 400 and nothing runs."""
    try:
        process_webhook(request.body, request.META.get('HTTP_X_PAYSTACK_SIGNATURE'))
    except InvalidWebhook:
        return HttpResponse(status=400)
    return HttpResponse(status=200)


# urls.py imports this name, so the Paystack webhook URL you registered does not change.
paystack_webhook = billing_webhook
















# # billing/views.py
# import logging

# from django.conf import settings
# from django.http import HttpResponse
# from rest_framework import permissions
# from rest_framework.decorators import (
#     api_view, authentication_classes, permission_classes, throttle_classes,
# )
# from rest_framework.response import Response
# from rest_framework.views import APIView

# from accounts.permissions import BusinessActivePermission
# from business.utils import get_user_business
# from .permissions import IsBusinessOwner
# from .plans import (
#     BILLING_INTERVALS, BUSINESS, CHECKOUT_METHODS, ENTERPRISE, FREE, INTERVAL_DISCOUNT_PERCENT, INTERVAL_LABELS,
#     INTERVAL_MONTHS, METHOD_CARD, MONTHLY, ONE_OFF_DAYS, PAID_TIERS, PLAN_FEATURES, PLAN_LIMITS, PLAN_NAMES,
#     TRIAL_DAYS, build_comparison, discount_label, get_price_ngn, has_live_card_subscription, is_checkout_pending,
#     monthly_equivalent_label, price_label, saving_label,
# )
# from .serializers import SubscriptionStatusSerializer
# from .services import (
#     BillingError, InvalidWebhook, ProviderUnavailable, confirm_checkout, create_checkout, ensure_subscription,
#     get_provider, get_subscription_manage_link, process_webhook,
# )

# logger = logging.getLogger(__name__)

# _NO_BUSINESS = {'error': 'No business associated with this account.'}
# _PROVIDER_DOWN = {'error': 'Billing is unavailable right now. Try again shortly.', 'code': 'billing_unavailable'}


# class SubscriptionStatusView(APIView):
#     """GET /api/billing/status/  plan, billing period, limits, usage and trial countdown."""
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         business = get_user_business(request.user)
#         if business is None:
#             return Response(_NO_BUSINESS, status=400)
#         sub = ensure_subscription(business)
#         return Response(SubscriptionStatusSerializer(sub, context={'business': business}).data)


# class PlansView(APIView):
#     """
#     GET /api/billing/plans/  what the plans page shows, on web and mobile.
#     Prices arrive as ready-made label strings (price_label, monthly_equivalent_label,
#     saving_label, discount_label) so the apps never format or hardcode an amount.
#     Each paid plan lists its billing periods, whether card checkout is set up for
#     each, and how many days a transfer or USSD purchase buys. `comparison` is the
#     plain feature table, built from the same limits the backend enforces.
#     Values come from billing/plans.py.
#     """
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         provider = get_provider()
#         plans = []
#         for tier in (FREE, BUSINESS, ENTERPRISE):
#             intervals = []
#             if tier in PAID_TIERS:
#                 monthly_total = get_price_ngn(tier, MONTHLY)
#                 for interval in BILLING_INTERVALS:
#                     price = get_price_ngn(tier, interval)
#                     months = INTERVAL_MONTHS[interval]
#                     intervals.append({
#                         'interval': interval,
#                         'label': INTERVAL_LABELS[interval],
#                         'months': months,
#                         'price_label': price_label(tier, interval),
#                         'monthly_equivalent_label': monthly_equivalent_label(tier, interval),
#                         'saving_label': saving_label(tier, interval),
#                         'discount_label': discount_label(interval),
#                         'discount_percent': INTERVAL_DISCOUNT_PERCENT[interval],
#                         # Numbers stay for apps that already read them. New code should use the labels.
#                         'price_ngn': price,
#                         'monthly_equivalent_ngn': round(price / months),
#                         'saving_ngn': monthly_total * months - price,
#                         'checkout_available': bool(provider.plan_code_for(tier, interval)),
#                         'one_off_available': True,
#                         'one_off_days': ONE_OFF_DAYS[interval],
#                     })
#             plans.append({
#                 'tier': tier,
#                 'name': PLAN_NAMES[tier],
#                 'limits': PLAN_LIMITS[tier],
#                 'features': PLAN_FEATURES[tier],
#                 'intervals': intervals,
#             })
#         return Response({
#             'plans': plans,
#             'comparison': build_comparison(),
#             'default_interval': MONTHLY,
#             'trial_days': TRIAL_DAYS,
#         })


# class BillingCheckoutView(APIView):
#     """
#     POST /api/billing/checkout/  Body: {"tier": "business" | "enterprise",
#     "interval": "monthly" | "biannually" | "annually",
#     "method": "card" | "transfer"}  (interval defaults to monthly, method to card)
#     Returns {"url": ..., "reference": ...}. Redirect the owner to url.

#     card is a recurring subscription. transfer is a one-off purchase through bank
#     transfer or USSD that buys 30, 180 or 365 days and does not renew by itself.
#     Both are refused while a card subscription is active.
#     """
#     permission_classes = [permissions.IsAuthenticated, BusinessActivePermission, IsBusinessOwner]

#     def post(self, request):
#         business = get_user_business(request.user)
#         if business is None:
#             return Response(_NO_BUSINESS, status=400)

#         tier = request.data.get('tier')
#         interval = request.data.get('interval') or MONTHLY
#         method = request.data.get('method') or METHOD_CARD
#         if tier not in PAID_TIERS:
#             return Response({'error': 'Unknown plan.', 'code': 'unknown_plan'}, status=400)
#         if interval not in BILLING_INTERVALS:
#             return Response({'error': 'Unknown billing period.', 'code': 'unknown_interval'}, status=400)
#         if method not in CHECKOUT_METHODS:
#             return Response({'error': 'Unknown payment method.', 'code': 'unknown_method'}, status=400)
#         if method == METHOD_CARD and not get_provider().plan_code_for(tier, interval):
#             return Response(
#                 {'error': 'That plan is not available for this billing period yet.', 'code': 'plan_unavailable'},
#                 status=400,
#             )

#         sub = ensure_subscription(business)

#         # A payment is confirmed and waiting for activation. A second checkout
#         # now would charge the customer twice.
#         if is_checkout_pending(sub):
#             return Response(
#                 {'error': 'We are still confirming your last payment. Check back in a few minutes.',
#                  'code': 'checkout_in_progress'},
#                 status=409,
#             )

#         if has_live_card_subscription(sub):
#             return Response(
#                 {'error': 'You already have an active subscription. Use the billing portal to manage it.',
#                  'code': 'already_subscribed',
#                  'plan': sub.plan_tier,
#                  'billing_interval': sub.billing_interval or None,
#                  'renews_at': sub.current_period_end},
#                 status=409,
#             )

#         try:
#             result = create_checkout(
#                 business=business, tier=tier, interval=interval, method=method,
#                 callback_url=settings.BILLING_SUCCESS_URL,
#             )
#         except ProviderUnavailable:
#             logger.exception('Billing checkout request failed')
#             return Response(_PROVIDER_DOWN, status=502)
#         except BillingError as e:
#             return Response({'error': str(e)}, status=502)
#         return Response({'url': result.url, 'reference': result.reference})


# class BillingCheckoutConfirmView(APIView):
#     """
#     POST /api/billing/checkout/confirm/  Body: {"reference": "..."}
#     The billing page calls this with the reference the provider put in the return
#     URL. It verifies the payment, then returns the same payload as /status/.
#     A transfer or USSD payment is applied here and the plan is active in the
#     response. A card payment comes back with checkoutPending true until the
#     webhook activates it, so keep polling /status/ until plan is business or
#     enterprise.
#     """
#     permission_classes = [permissions.IsAuthenticated, BusinessActivePermission, IsBusinessOwner]

#     def post(self, request):
#         business = get_user_business(request.user)
#         if business is None:
#             return Response(_NO_BUSINESS, status=400)

#         reference = request.data.get('reference')
#         try:
#             sub = confirm_checkout(business, reference)
#         except ValueError as e:
#             return Response({'error': str(e), 'code': 'invalid_reference'}, status=400)
#         except ProviderUnavailable:
#             logger.exception('Billing verify request failed')
#             return Response(_PROVIDER_DOWN, status=502)
#         except BillingError as e:
#             return Response({'error': str(e), 'code': 'payment_not_confirmed'}, status=400)
#         return Response(SubscriptionStatusSerializer(sub, context={'business': business}).data)


# class BillingPortalView(APIView):
#     """POST /api/billing/portal/  only valid for a card subscription that already exists."""
#     permission_classes = [permissions.IsAuthenticated, BusinessActivePermission, IsBusinessOwner]

#     def post(self, request):
#         business = get_user_business(request.user)
#         if business is None:
#             return Response(_NO_BUSINESS, status=400)
#         try:
#             url = get_subscription_manage_link(business)
#         except ProviderUnavailable:
#             logger.exception('Billing portal request failed')
#             return Response(_PROVIDER_DOWN, status=502)
#         except BillingError as e:
#             return Response({'error': str(e)}, status=400)
#         return Response({'url': url})


# @api_view(['POST'])
# @authentication_classes([])
# @permission_classes([permissions.AllowAny])
# @throttle_classes([])  # the provider calls from a few IPs, the anonymous rate limit would reject real events
# def billing_webhook(request):
#     """The signature is verified inside the provider on every call. A bad one gets 400 and nothing runs."""
#     try:
#         process_webhook(request.body, request.META.get('HTTP_X_PAYSTACK_SIGNATURE'))
#     except InvalidWebhook:
#         return HttpResponse(status=400)
#     return HttpResponse(status=200)


# # urls.py imports this name, so the Paystack webhook URL you registered does not change.
# paystack_webhook = billing_webhook
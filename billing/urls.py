# billing/urls.py
from django.urls import path
from .views import (
    BillingCheckoutConfirmView, BillingCheckoutView, BillingPortalView, PlansView,
    SubscriptionStatusView, paystack_webhook,
)

urlpatterns = [
    path('status/',           SubscriptionStatusView.as_view(),   name='billing-status'),
    path('plans/',            PlansView.as_view(),                name='billing-plans'),
    path('checkout/',         BillingCheckoutView.as_view(),      name='billing-checkout'),
    path('checkout/confirm/', BillingCheckoutConfirmView.as_view(), name='billing-checkout-confirm'),
    path('portal/',           BillingPortalView.as_view(),        name='billing-portal'),
    path('webhook/',          paystack_webhook,                   name='paystack-webhook'),
]
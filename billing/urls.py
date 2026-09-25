# billing/urls.py
from django.urls import path
from .views import SubscriptionStatusView, BillingCheckoutView, BillingPortalView, paystack_webhook

urlpatterns = [
    path('status/',   SubscriptionStatusView.as_view(), name='billing-status'),
    path('checkout/', BillingCheckoutView.as_view(),     name='billing-checkout'),
    path('portal/',   BillingPortalView.as_view(),       name='billing-portal'),
    path('webhook/',  paystack_webhook,                  name='paystack-webhook'),
]
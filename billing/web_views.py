# billing/web_views.py
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages

from business.utils import get_user_business
from .models import Subscription
from .services import create_subscription_checkout, get_subscription_manage_link, PaystackError


def login_view(request):
    if request.user.is_authenticated:
        return redirect('billing-dashboard')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')
        user = authenticate(request, username=email, password=password)
        if user is None:
            messages.error(request, 'Invalid email or password.')
        elif user.role != 'owner':
            messages.error(request, 'Only business owners can manage billing.')
        else:
            login(request, user)
            return redirect('billing-dashboard')

    return render(request, 'billing/login.html')


def logout_view(request):
    logout(request)
    return redirect('billing-login')


@login_required(login_url='billing-login')
def dashboard_view(request):
    business = get_user_business(request.user)
    if business is None or request.user.role != 'owner':
        messages.error(request, 'No business found for this account.')
        return redirect('billing-login')

    sub, _ = Subscription.objects.get_or_create(business=business)

    if sub.status == Subscription.Status.TRIALING:
        status_message = (
            f"You're on a free trial until {sub.trial_ends_at:%d %b %Y}."
            if sub.trial_ends_at else "You're on a free trial."
        )
        cta_url, cta_label = 'billing-plans', 'Subscribe now'
    elif sub.status == Subscription.Status.ACTIVE:
        status_message = "Your subscription is active."
        cta_url, cta_label = 'billing-portal', 'Manage billing'
    elif sub.status == Subscription.Status.PAST_DUE:
        status_message = "Your last payment failed. Update your billing to avoid losing access."
        cta_url, cta_label = 'billing-portal', 'Update payment method'
    else:  # canceled
        status_message = "Your subscription is inactive."
        cta_url, cta_label = 'billing-plans', 'Subscribe now'

    return render(request, 'billing/dashboard.html', {
        'business': business,
        'subscription': sub,
        'status_message': status_message,
        'cta_url': cta_url,
        'cta_label': cta_label,
    })


@login_required(login_url='billing-login')
def plans_view(request):
    business = get_user_business(request.user)
    return render(request, 'billing/plans.html', {
        'business': business,
        'plans': [
            {'tier': 'basic', 'label': 'Basic', 'price': '₦9,000/mo'},
            {'tier': 'pro',   'label': 'Pro',   'price': '₦29,000/mo'},
        ],
    })


@login_required(login_url='billing-login')
def start_checkout(request, tier: str):
    business = get_user_business(request.user)
    plan_code = settings.PAYSTACK_PLAN_CODES.get(tier)
    if not plan_code:
        messages.error(request, 'Unknown plan.')
        return redirect('billing-plans')
    try:
        checkout_url = create_subscription_checkout(
            business=business, plan_code=plan_code,
            callback_url=request.build_absolute_uri('/billing/success/'),
        )
    except PaystackError:
        messages.error(request, 'Could not start checkout right now. Try again shortly.')
        return redirect('billing-plans')
    return redirect(checkout_url)


@login_required(login_url='billing-login')
def open_portal(request):
    business = get_user_business(request.user)
    try:
        portal_url = get_subscription_manage_link(business)
    except PaystackError:
        messages.error(request, 'No active subscription to manage yet.')
        return redirect('billing-dashboard')
    return redirect(portal_url)


def checkout_success(request):
    return render(request, 'billing/success.html')


def checkout_cancel(request):
    return render(request, 'billing/cancel.html')
# billing/emails.py
import logging

import resend
from django.conf import settings
from django.utils.html import escape

resend.api_key = settings.RESEND_API_KEY
logger = logging.getLogger(__name__)

# The Next.js billing page. Built from FRONTEND_URL, so it follows the domain
# you set there (https://app.billbuzz.ng).
BILLING_PAGE_PATH = '/billing'


def _billing_url() -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{BILLING_PAGE_PATH}"


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


def _send(business, subject: str, paragraphs: list) -> bool:
    owner = business.owner
    name = escape(owner.first_name or business.name)
    body = ''.join(f'<p>{p}</p>' for p in paragraphs)
    try:
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [owner.email],
            "subject": subject,
            "html": f'<p>Hello {name},</p>{body}<p>The BillBuzz team</p>',
        })
        return True
    except Exception:
        logger.exception(f'Failed to send billing email to {owner.email}')
        return False


def send_payment_failed_email(business) -> bool:
    return _send(business, 'Action needed: your BillBuzz payment failed', [
        "We could not process your latest BillBuzz payment, so your account has moved to the Free plan for now. "
        "Everything you created is still here, but the Free plan has lower limits.",
        f'Update your billing and your plan comes back as soon as a payment goes through: '
        f'<a href="{_billing_url()}">{_billing_url()}</a>',
    ])


def send_subscription_canceled_email(business) -> bool:
    return _send(business, 'Your BillBuzz subscription has ended', [
        "Your subscription has ended and your account is now on the Free plan. "
        "Your invoices, products and customers are all still here, and you can keep using BillBuzz within the Free limits.",
        f'Resubscribe whenever you are ready: <a href="{_billing_url()}">{_billing_url()}</a>',
    ])


def send_trial_ending_soon_email(business, days_left: int) -> bool:
    return _send(business, f'Your BillBuzz trial ends in {_days(days_left)}', [
        f"Your free trial ends in {_days(days_left)}. After that your account moves to the Free plan, "
        "which has lower limits on invoices, products and staff.",
        f'Choose a plan now to keep everything running as it is: <a href="{_billing_url()}">{_billing_url()}</a>',
    ])


def send_plan_ending_soon_email(business, days_left: int) -> bool:
    """For a paid plan that is set not to renew."""
    return _send(business, f'Your BillBuzz plan ends in {_days(days_left)}', [
        f"Your plan is set not to renew and ends in {_days(days_left)}. After that your account moves to the "
        "Free plan, which has lower limits.",
        f'Changed your mind? You can keep your plan from your billing page: '
        f'<a href="{_billing_url()}">{_billing_url()}</a>',
    ])


def send_plan_renewal_soon_email(business, days_left: int, plan_name: str) -> bool:
    """For a paid plan that renews automatically."""
    return _send(business, f'Your BillBuzz {escape(plan_name)} plan renews in {_days(days_left)}', [
        f"Your {escape(plan_name)} plan renews in {_days(days_left)}. Make sure the card on file can cover the payment. "
        "If a payment fails, your account moves to the Free plan until it goes through.",
        f'You can check your billing details here: <a href="{_billing_url()}">{_billing_url()}</a>',
    ])

def send_one_off_renewal_reminder_email(business, days_left: int, plan_name: str) -> bool:
    """For a plan paid by transfer or USSD. It never renews by itself, so the owner has to pay again."""
    return _send(business, f'Your BillBuzz {escape(plan_name)} plan ends in {_days(days_left)}', [
        f"Your {escape(plan_name)} plan ends in {_days(days_left)}. Plans paid by transfer or USSD do not renew "
        "by themselves, so you need to pay again to keep going. After that your account moves to the Free plan, "
        "which has lower limits.",
        f'Renew from your billing page and the new time is added after your current period: '
        f'<a href="{_billing_url()}">{_billing_url()}</a>',
    ])


def send_payment_received_email(business, plan_name: str, ends_on: str) -> bool:
    """Sent once when a transfer or USSD payment has been applied."""
    return _send(business, f'We received your BillBuzz {escape(plan_name)} payment', [
        f"Thank you. Your {escape(plan_name)} plan is paid through {escape(ends_on)}.",
        f'You can see your plan and renew any time here: <a href="{_billing_url()}">{_billing_url()}</a>',
    ])
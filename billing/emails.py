# billing/emails.py
import logging
from django.core.mail import send_mail
from django.conf import settings

logger = logging.getLogger(__name__)


def _send(business, subject: str, message: str) -> None:
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[business.owner.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception(f'Failed to send billing email to {business.owner.email}')


def send_payment_failed_email(business) -> None:
    _send(business, 'Action needed: your BillBuzz payment failed',
        f"Hi {business.owner.first_name or business.name},\n\n"
        "We couldn't process your latest BillBuzz payment. You have a short grace "
        "period before your account is locked.\n\n"
        "Log in to update your billing: https://app.billbuzz.ng/billing/\n\n— BillBuzz")


def send_subscription_canceled_email(business) -> None:
    _send(business, 'Your BillBuzz subscription has ended',
        f"Hi {business.owner.first_name or business.name},\n\n"
        "Your BillBuzz account is now locked. Log in below to resubscribe whenever "
        "you're ready:\n\nhttps://app.billbuzz.ng/billing/\n\n— BillBuzz")


def send_trial_ending_soon_email(business, days_left: int) -> None:
    _send(business, f'Your BillBuzz trial ends in {days_left} day{"s" if days_left != 1 else ""}',
        f"Hi {business.owner.first_name or business.name},\n\n"
        f"Your free trial ends in {days_left} day{'s' if days_left != 1 else ''}. Subscribe "
        "now to avoid any interruption:\n\nhttps://app.billbuzz.ng/billing/plans/\n\n— BillBuzz")


def send_grace_period_ending_soon_email(business, hours_left: int) -> None:
    _send(business, 'Your BillBuzz account will be locked soon',
        f"Hi {business.owner.first_name or business.name},\n\n"
        f"Your account locks in under {hours_left} hours due to a failed payment. "
        "Update billing now: https://app.billbuzz.ng/billing/\n\n— BillBuzz")
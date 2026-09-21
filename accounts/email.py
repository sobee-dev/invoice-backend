import resend
from django.conf import settings

resend.api_key = settings.RESEND_API_KEY

def send_password_reset_email(user, reset_url: str) -> bool:
    try:
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,      # e.g. "BillBuzz <noreply@billbuzz.app>"
            "to": [user.email],
            "subject": "Reset your BillBuzz password",
            "html": f"""
                <p>Hello {user.first_name or ''},</p>
                <p>Click below to reset your password. This link expires in 30 minutes
                and can only be used once.</p>
                <p><a href="{reset_url}">Reset Password</a></p>
                <p>If you didn't request this, you can safely ignore this email.</p>
            """,
        })
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Resend email failed for {user.email}: {e}")
        return False


def send_account_deletion_scheduled_email(user, scheduled_for) -> bool:
    try:
        formatted_date = scheduled_for.strftime('%B %d, %Y')
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [user.email],
            "subject": "Your BillBuzz account is scheduled for deletion",
            "html": f"""
                <p>Hello {user.first_name or ''},</p>
                <p>We've received a request to delete your BillBuzz account. Your account
                has been deactivated and will be permanently deleted on
                <strong>{formatted_date}</strong>.</p>
                <p>Changed your mind? You can cancel this any time before then by opening
                the BillBuzz app and entering your email and passcode on the
                "Reactivate Account" screen — no need to reply to this email.</p>
                <p>If you didn't request this, please reactivate your account immediately
                and contact support, as someone else may have access to it.</p>
            """,
        })
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Resend deletion-scheduled email failed for {user.email}: {e}")
        return False   
    
    
def send_account_suspended_email(user, reason: str) -> bool:
    try:
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [user.email],
            "subject": "Your BillBuzz account has been suspended",
            "html": f"""
                <p>Hello {user.first_name or ''},</p>
                <p>Your BillBuzz account has been suspended.</p>
                <p><strong>Reason:</strong> {reason}</p>
                <p>You've been signed out of all devices and won't be able to log back in
                until this is resolved. If you believe this was done in error, please
                contact support.</p>
            """,
        })
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Resend suspension email failed for {user.email}: {e}")
        return False    
    

def send_onboarding_email(user) -> bool:
    try:
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [user.email],
            "subject": "Welcome to BillBuzz",
            "html": f"""
                <p>Hello {user.first_name or ''},</p>
                <p>Welcome to BillBuzz — you're all set up. Here's how to get moving:</p>
                <ol>
                    <li>Finish setting up your business profile (name, logo, currency, tax rate)</li>
                    <li>Pick an invoice template</li>
                    <li>Create your first invoice or quote</li>
                </ol>
                <p>If you have any questions along the way, just reply to this email.</p>
            """,
        })
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Resend onboarding email failed for {user.email}: {e}")
        return False    
    
    
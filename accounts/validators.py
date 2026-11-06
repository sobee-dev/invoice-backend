# accounts/validators.py
import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

PASSCODE_LENGTH = 6
_PASSCODE_PATTERN = re.compile(rf"[0-9]{{{PASSCODE_LENGTH}}}")


from disposable_email_domains import blocklist
from rest_framework import serializers


def is_disposable_email(email: str) -> bool:
    domain = email.rsplit('@', 1)[-1].strip().lower()
    parts = domain.split('.')
    # Check the domain and its parents, so "x.mailinator.com" is caught
    # but a bare TLD is never compared.
    return any('.'.join(parts[i:]) in blocklist for i in range(len(parts) - 1))


def reject_disposable_email(value: str) -> str:
    if is_disposable_email(value):
        raise serializers.ValidationError(
            "Disposable email addresses aren't allowed. Please use a permanent email."
        )
    return value

def validate_passcode_format(password, user=None):
    """
    The app's own rule: a password is exactly six digits.

    Superusers are exempt so the admin dashboard and the Django admin can use
    stronger passwords. This is deliberately NOT listed in
    AUTH_PASSWORD_VALIDATORS, because that list also runs for createsuperuser
    and the admin password forms. Login never calls this, so accounts created
    before the rule existed can still sign in.
    """
    if user is not None and getattr(user, "is_superuser", False):
        return

    # fullmatch with [0-9] rejects letters, spaces, trailing newlines and
    # non ASCII digits that str.isdigit() would let through.
    if not _PASSCODE_PATTERN.fullmatch(password or ""):
        raise ValidationError(
            _("Password must be exactly %(n)d digits.") % {"n": PASSCODE_LENGTH},
            code="passcode_format",
        )


def validate_new_password(password, user=None):
    """
    The single entry point for every place a password is created or changed
    (register, change password, reset confirm, and staff creation if it sets
    a password). Format rule first, then Django's validators from settings.
    Raises django.core.exceptions.ValidationError, so callers catch that one.
    """
    validate_passcode_format(password, user)
    validate_password(password, user=user)


class NoCommonPinValidator:
    """
    Blocks highly insecure or sequential 6-digit PINs/passwords
    while allowing custom numeric combinations.
    """
    def validate(self, password, user=None):
        # Only target numeric strings (or handle all, but specifically 6-digits)
        if password.isdigit():
            # 1. Check for repeating digits (e.g., 000000, 111111)
            if len(set(password)) == 1:
                raise ValidationError(
                    _("This password is too simple. Avoid repeating single digits."),
                    code="password_too_simple",
                )

            # 2. Check for ascending sequences (e.g., 123456, 012345)
            ascending = "01234567890123456789"
            if password in ascending:
                raise ValidationError(
                    _("This password is a sequential number pattern and is too easy to guess."),
                    code="password_sequential",
                )

            # 3. Check for descending sequences (e.g., 654321, 987654)
            descending = "98765432109876543210"
            if password in descending:
                raise ValidationError(
                    _("This password is a sequential number pattern and is too easy to guess."),
                    code="password_sequential",
                )

    def get_help_text(self):
        return _("Your 6-digit password cannot be entirely repeating digits or standard sequences like 123456.")
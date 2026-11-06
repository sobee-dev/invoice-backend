# accounts/google.py
import logging
import os
import time

import requests
from django.conf import settings
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import status
from rest_framework.response import Response

from django.contrib.auth.hashers import make_password
from django.core import signing

from .models import AccountStatus, User
from .terms import issue_signup_token, read_signup_token, terms_fields

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
VALID_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# How recent a Google sign in must be to count as proof for a sensitive action.
REAUTH_MAX_AGE_SECONDS = 300


class GoogleAuthError(Exception):
    """The message is safe to show the user. status_code is for views that return it directly."""

    def __init__(self, message, status_code=400, code=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def as_response(self):
        body = {"error": self.message}
        if self.code:
            body["code"] = self.code
        return Response(body, status=self.status_code)


def has_real_password(user) -> bool:
    """
    False for accounts that only ever signed in with Google. Accounts created
    by get_or_create carry an empty string, which Django still calls usable,
    so the emptiness check matters.
    """
    return bool(user.password) and user.has_usable_password()


def is_staff_account(user) -> bool:
    """
    True for anyone linked to a business as staff, active or deactivated.
    Staff sign in with email and password only, never with Google.
    """
    return user.role == 'staff' or user.staff_accounts.exists()


def _mobile_audiences():
    values = (
        os.getenv("GOOGLE_IOS_CLIENT_ID"),
        os.getenv("GOOGLE_ANDROID_CLIENT_ID"),
        os.getenv("GOOGLE_WEB_CLIENT_ID"),
    )
    return {v for v in values if v}


def _check_claims(user_info):
    if user_info.get("iss") not in VALID_ISSUERS:
        raise GoogleAuthError("Invalid token issuer.")
    email = (user_info.get("email") or "").lower()
    if not email or not user_info.get("email_verified", False):
        raise GoogleAuthError("Google email is missing or unverified.")
    return {**user_info, "email": email}


def exchange_code(code):
    """Web: trade an authorization code for verified user info. The client secret stays on the server."""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error("Google web sign-in called but GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET is missing.")
        raise GoogleAuthError("Google sign-in is not configured.", 503)

    # The redirect URI comes from settings, never from the client, and it must
    # match the one the button sent to Google character for character.
    try:
        token_res = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
    except requests.RequestException:
        raise GoogleAuthError("Could not reach Google. Please try again.", 502)

    if token_res.status_code != 200:
        # Typical causes: invalid_grant (code reused or expired) and redirect_uri_mismatch.
        logger.warning("Google code exchange failed: %s", token_res.text)
        raise GoogleAuthError("Google rejected the sign-in attempt. Please try again.")

    raw_id_token = token_res.json().get("id_token")
    if not raw_id_token:
        raise GoogleAuthError("Google did not return an identity token.")

    try:
        info = id_token.verify_oauth2_token(
            raw_id_token,
            google_requests.Request(),
            audience=client_id,  # pinned to this exact client
        )
    except ValueError as e:
        logger.warning("Google id token verification failed: %s", e)
        raise GoogleAuthError("Google sign-in could not be verified.")

    return _check_claims(info)


def verify_mobile_id_token(raw_id_token):
    """Mobile: verify an id token handed over by the native Google SDK."""
    try:
        info = id_token.verify_oauth2_token(raw_id_token, google_requests.Request())
    except ValueError as e:
        logger.warning("Google mobile id token verification failed: %s", e)
        raise GoogleAuthError("Google sign-in could not be verified.")

    if info.get("aud") not in _mobile_audiences():
        raise GoogleAuthError("Token was not issued for this app.")

    return _check_claims(info)


def user_info_from_proof(code=None, raw_id_token=None):
    if code:
        return exchange_code(code)
    if raw_id_token:
        return verify_mobile_id_token(raw_id_token)
    raise GoogleAuthError("Please confirm with Google to continue.")


def require_recent(user_info, max_age_seconds=REAUTH_MAX_AGE_SECONDS):
    """Sensitive actions need a sign in from the last few minutes, not an old token."""
    issued_at = user_info.get("iat")
    if not isinstance(issued_at, (int, float)) or time.time() - issued_at > max_age_seconds:
        raise GoogleAuthError("That Google sign-in is too old. Please confirm with Google again.")


def login_gate_response(user, business):
    """
    The same checks password login makes, shared by the web and mobile Google
    views so a Google sign in cannot sidestep a suspension or a scheduled
    deletion. Returns a Response to send back, or None when the user may sign in.
    Staff are refused first: once an email is linked to a business as staff it
    can only sign in with email and password.
    """
    if is_staff_account(user):
        return Response(
            {
                "error": "Staff accounts sign in with email and password. Google sign-in is not available for them.",
                "code": "staff_google_login_not_allowed",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if user.status == AccountStatus.SUSPENDED:
        return Response(
            {"error": "This account has been suspended. Contact support.", "code": "suspended"},
            status=status.HTTP_403_FORBIDDEN,
        )
    if user.status == AccountStatus.PENDING_DELETION:
        return Response(
            {
                "error": "This account is scheduled for deletion.",
                "code": "pending_deletion",
                "deletionScheduledFor": user.deletion_scheduled_for,
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if not user.is_active:
        return Response(
            {"error": "Account is disabled.", "code": "disabled"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def identity_from_signup_token(token):
    """Second step of a new sign-up: the identity Google already verified, carried in a signed token."""
    try:
        data = read_signup_token(token)
    except signing.BadSignature:
        raise GoogleAuthError(
            "This sign-up link expired. Please sign in with Google again.",
            400, code="signup_expired",
        )
    return {
        "email": data["email"],
        "given_name": data["first_name"],
        "family_name": data["last_name"],
    }


def resolve_google_user(user_info, accepted_terms):
    """
    Returns (user, is_new, blocked_response). Existing accounts sign in as
    before. A new account is only created once the terms were accepted,
    otherwise the caller gets a 403 terms_required carrying a signed token
    so the single-use Google code is not needed a second time.
    """
    email = user_info["email"]
    user = User.objects.filter(email=email).first()
    if user is not None:
        return user, False, None

    first_name = user_info.get("given_name", "")
    last_name = user_info.get("family_name", "")

    if not accepted_terms:
        return None, True, Response(
            {
                "error": "Please accept the Terms of Service and Privacy Policy to create your account.",
                "code": "terms_required",
                "signup_token": issue_signup_token(email, first_name, last_name),
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    # get_or_create so two simultaneous requests can't both insert the email.
    user, is_new = User.objects.get_or_create(
        email=email,
        defaults={
            "first_name": first_name,
            "last_name": last_name,
            "is_active": True,
            "password": make_password(None),  # unusable, so has_real_password is False
            **terms_fields(),
        },
    )
    return user, is_new, None
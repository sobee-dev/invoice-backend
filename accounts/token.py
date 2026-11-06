# accounts/token.py
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

MAX_SESSION_AGE = timedelta(days=14)

# The refresh cookie must never share a name with the access token cookie
# that CookieJWTAuthentication used to look for, so keep this distinct.
REFRESH_COOKIE_NAME = settings.SIMPLE_JWT.get("AUTH_REFRESH_COOKIE", "refresh_token")
REFRESH_COOKIE_PATH = settings.SIMPLE_JWT.get("AUTH_COOKIE_PATH", "/api/users/")
REFRESH_COOKIE_SAMESITE = settings.SIMPLE_JWT.get("AUTH_COOKIE_SAMESITE", "Lax")


def is_web_client(request) -> bool:
    """The Next.js app sends X-Client: web on every request. Mobile does not."""
    return request.META.get("HTTP_X_CLIENT", "").lower() == "web"


def session_deadline_ms(refresh) -> int:
    """Absolute end of the session in ms epoch, derived from orig_iat."""
    orig_iat = refresh.get("orig_iat")
    if orig_iat is None:
        orig_iat = int(timezone.now().timestamp())
    return int((orig_iat + MAX_SESSION_AGE.total_seconds()) * 1000)


def set_refresh_cookie(response, refresh_token: str) -> None:
    lifetime = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=int(lifetime.total_seconds()),
        httponly=True,
        secure=settings.SIMPLE_JWT.get("AUTH_COOKIE_SECURE", True),
        samesite=REFRESH_COOKIE_SAMESITE,
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        samesite=REFRESH_COOKIE_SAMESITE,
    )


def build_session_response(request, *, access: str, refresh, payload=None,
                           status_code=status.HTTP_200_OK) -> Response:
    """
    One place that decides how tokens leave the server.
    Web: refresh goes into an httpOnly cookie, body carries access only.
    Mobile (no X-Client header): refresh stays in the body as before.
    """
    data = dict(payload or {})
    data["access"] = access
    data["session_expires_at"] = session_deadline_ms(refresh)  # camelCased by the renderer

    web = is_web_client(request)
    if not web:
        data["refresh"] = str(refresh)

    response = Response(data, status=status_code)
    if web:
        set_refresh_cookie(response, str(refresh))
    return response


def issue_session(request, user, payload=None, status_code=status.HTTP_200_OK) -> Response:
    """Mint a brand new session for a user (login, register, google, reactivation)."""
    refresh = RefreshToken.for_user(user)
    refresh["orig_iat"] = int(timezone.now().timestamp())
    return build_session_response(
        request,
        access=str(refresh.access_token),
        refresh=refresh,
        payload=payload,
        status_code=status_code,
    )


class SessionLimitedTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        refresh = RefreshToken(attrs["refresh"])
        orig_iat = refresh.get("orig_iat")

        # Tokens issued before this feature shipped won't have the claim,
        # treat those as already expired rather than granting them an
        # unbounded session by default.
        if orig_iat is None:
            raise serializers.ValidationError("Session expired. Please log in again.")

        issued_at = datetime.fromtimestamp(orig_iat, tz=dt_timezone.utc)
        if datetime.now(dt_timezone.utc) - issued_at > MAX_SESSION_AGE:
            raise serializers.ValidationError("Session expired. Please log in again.")

        return super().validate(attrs)
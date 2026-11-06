# accounts/authenticate.py
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class CookieJWTAuthentication(JWTAuthentication):
    """
    Name kept so REST_FRAMEWORK settings do not change, but it now reads the
    access token from the Authorization header only. Web keeps the access
    token in memory and the refresh token in an httpOnly cookie that is
    only ever sent to /api/users/, so there is no access cookie to read.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            return None

        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
            return self.get_user(validated_token), validated_token
        except (InvalidToken, TokenError, AuthenticationFailed):
            # Expired, malformed, or inactive user: fall through to a 401
            # from the permission classes so the client can try a refresh.
            return None
# accounts/authenticate.py
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.conf import settings

class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        # 1. Look for the cookie name in settings
        cookie_name = settings.SIMPLE_JWT.get('AUTH_COOKIE', 'access')
        raw_token = request.COOKIES.get(cookie_name)
        
        # 2. Hybrid Support: Check the 'Authorization' header if the cookie is missing
        # This is helpful for debugging via Postman or Insomnia
        if not raw_token:
            header = self.get_header(request)
            if header:
                raw_token = self.get_raw_token(header)

        # 3. If still no token, return None (Standard DRF behavior)
        if raw_token is None:
            return None

        # 4. Use a try/except block to catch expired or malformed tokens
        try:
            validated_token = self.get_validated_token(raw_token)
            return self.get_user(validated_token), validated_token
        except:
            # If token is invalid, returning None allows the 'PermissionDenied' 
            # to be handled gracefully by the View's permission_classes
            return None
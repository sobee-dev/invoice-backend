# accounts/throttles.py
from rest_framework.throttling import AnonRateThrottle

class PasswordResetThrottle(AnonRateThrottle):
    scope = 'password_reset'
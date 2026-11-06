# accounts/google_web.py
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from business.serializers import BusinessSerializer
from business.utils import get_user_business

from .google import (
    GoogleAuthError,
    exchange_code,
    identity_from_signup_token,
    login_gate_response,
    resolve_google_user,
)
from .serializers import UserSerializer
from .token import issue_session


class GoogleWebCodeLoginView(APIView):
    """
    POST /api/users/google_web/

    First call:  { "code": "..." }
    If the email is new and terms were not accepted, replies 403
    { code: "terms_required", signupToken }.
    Second call: { "signupToken": "...", "acceptedTerms": true }
    A first call may also include acceptedTerms: true up front.

    Web only. The refresh token lands in the httpOnly cookie via issue_session.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        signup_token = request.data.get("signup_token")
        code = request.data.get("code")
        accepted_terms = request.data.get("accepted_terms") is True

        if not signup_token and not code:
            return Response(
                {"error": "Authorization code is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user_info = (
                identity_from_signup_token(signup_token) if signup_token else exchange_code(code)
            )
        except GoogleAuthError as e:
            return e.as_response()

        user, is_new, needs_terms = resolve_google_user(user_info, accepted_terms)
        if needs_terms:
            return needs_terms

        business = get_user_business(user)

        blocked = login_gate_response(user, business)
        if blocked:
            return blocked

        return issue_session(request, user, {
            "user": UserSerializer(user).data,
            "business": BusinessSerializer(business).data if business else None,
            "requires_password_change": user.requires_password_change,
            "isNew": is_new,
            "message": "Google login successful",
        })
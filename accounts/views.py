# accounts/views.py

from django.views.generic import TemplateView
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.views import APIView
from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
import os

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from django.db import transaction
from accounts.email import send_onboarding_email, send_password_reset_email
from accounts.google import GoogleAuthError, identity_from_signup_token, login_gate_response, resolve_google_user, verify_mobile_id_token
from accounts.throttles import PasswordResetThrottle
from billing.exceptions import STAFF_BLOCKED_MESSAGE
from billing.plans import staff_access_blocked
from business.models import Business
from business.serializers import BusinessSerializer
from business.utils import get_user_business

from django.conf import settings
from .services import set_account_status
from .models import AccountStatus, AccountStatusAudit, PasswordResetToken, User
from .serializers import (
    AccountDeletionRequestSerializer,
    AccountStatusAuditSerializer,
    AdminAccountStatusSerializer,
    CancelAccountDeletionSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    UserSerializer,
    UserRegistrationSerializer,
    UserUpdateSerializer,
    ChangePasswordSerializer,
    UserListSerializer,
    UserWithBusinessSerializer,
    AdminDashboardSerializer,
)

from rest_framework_simplejwt.views import TokenRefreshView
from .token import (
    REFRESH_COOKIE_NAME,
    SessionLimitedTokenRefreshSerializer,
    build_session_response,
    clear_refresh_cookie,
    is_web_client,
    issue_session,
)
from django.http import HttpResponse


class UserViewSet(viewsets.ModelViewSet):
    """
    ViewSet for User model handling Authentication and Profile management.
    """
    queryset = User.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action in ['create', 'register']:
            return UserRegistrationSerializer
        elif self.action in ['update', 'partial_update', 'update_notifications']:
            return UserUpdateSerializer
        elif self.action == 'list':
            return UserListSerializer
        elif self.action == 'with_business':
            return UserWithBusinessSerializer
        elif self.action == 'change_password':
            return ChangePasswordSerializer
        elif self.action == 'admin_dashboard_stats':
            return AdminDashboardSerializer
        elif self.action == 'request_password_reset':
            return PasswordResetRequestSerializer
        elif self.action == 'confirm_password_reset':
            return PasswordResetConfirmSerializer
        elif self.action == 'request_account_deletion':
            return AccountDeletionRequestSerializer
        elif self.action == 'set_status':
            return AdminAccountStatusSerializer
        elif self.action == 'cancel_account_deletion':
            return CancelAccountDeletionSerializer
        return UserSerializer

    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in [
            'register', 'login', 'logout',
            'request_password_reset', 'confirm_password_reset',
            'cancel_account_deletion',
        ]:
            return [permissions.AllowAny()]
        # Fall back to the class or @action level permission_classes so that
        # IsAdminUser on set_status, status_audit and admin_dashboard_stats
        # is actually enforced.
        return super().get_permissions()

    def get_queryset(self):
        """Filter queryset: Admins see all, users see only themselves"""
        if self.request.user.is_authenticated:
            if self.request.user.is_staff:
                return User.objects.all()
            return User.objects.filter(id=self.request.user.id)
        return User.objects.none()

    # ========== REGISTER ==========
    @action(detail=False, methods=['post'])
    def register(self, request):
        """
        POST /api/users/register/
        Register a new user account
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        transaction.on_commit(lambda: send_onboarding_email(user))

        return issue_session(
            request,
            user,
            {
                'user': UserSerializer(user).data,
                'requires_password_change': user.requires_password_change,
                'message': 'User registered successfully',
            },
            status_code=status.HTTP_201_CREATED,
        )

    # ========== LOGIN ==========
    @action(detail=False, methods=['post'])
    def login(self, request):
        """
        POST /api/users/login/

        Accepts:
          - email + password (standard login)

        Returns:
          - access token
          - refresh token (body for mobile, httpOnly cookie for web)
          - user data
          - requires_password_change flag
          - business data (if owner)
        """
        email = request.data.get('email')
        password = request.data.get('password')

        if not email:
            return Response(
                {'error': 'Email is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not password:
            return Response(
                {'error': 'Password is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        email = email.lower()
        user = authenticate(request, email=email, password=password)

        if user is None:
            # Django's auth backends refuse inactive users outright, and
            # suspended or pending deletion accounts are inactive by design.
            # Look the account up directly, but only accept it once the
            # password checks out, so this never reveals which emails exist.
            candidate = User.objects.filter(email=email).first()
            if candidate is None or not candidate.check_password(password):
                return Response(
                    {'error': 'Invalid email or password.'},
                    status=status.HTTP_401_UNAUTHORIZED
                )
            user = candidate

        if user.status == AccountStatus.SUSPENDED:
            return Response(
                {'error': 'This account has been suspended. Contact support.', 'code': 'suspended'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if user.status == AccountStatus.PENDING_DELETION:
            return Response(
                {
                    'error': 'This account is scheduled for deletion.',
                    'code': 'pending_deletion',
                    'deletionScheduledFor': user.deletion_scheduled_for,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response({'error': 'Account is disabled.', 'code': 'disabled'}, status=status.HTTP_403_FORBIDDEN)

        # Get business and docs (for owners)
        business = get_user_business(user)

        if user.role == 'staff':
            if business and business.owner.status == AccountStatus.SUSPENDED:
                return Response(
                    {'error': 'This business account has been suspended. Contact the business owner or support.', 'code': 'business_suspended'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            # The newest staff over the plan's staff cap cannot sign in.
            if staff_access_blocked(user):
                return Response(
                    {'error': STAFF_BLOCKED_MESSAGE, 'code': 'staff_over_plan_limit'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        return issue_session(request, user, {
            'user': UserSerializer(user).data,
            'business': BusinessSerializer(business).data if business else None,
            'requires_password_change': user.requires_password_change,
            'message': 'Login successful',
        })

    # ========== CHANGE PASSWORD ==========
    @action(detail=False, methods=['post'])
    def change_password(self, request):
        """
        POST /api/users/change_password/

        Staff changes password (required on first login).
        On first login, old_password is not required.

        Request:
          - old_password (optional on first login, required after)
          - new_password
          - new_password_confirm
        """
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({
            'message': 'Password changed successfully'
        }, status=status.HTTP_200_OK)

    # ========== VERIFY EMAIL ==========
    @action(detail=False, methods=['post'])
    def verify_email(self, request):
        """POST /api/users/verify_email/"""
        user = request.user
        user.email_verified_at = timezone.now()
        user.save()

        return Response({
            'message': 'Email verified successfully',
            'email_verified_at': user.email_verified_at
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='password_reset/request', throttle_classes=[PasswordResetThrottle])
    def request_password_reset(self, request):
        """
        POST /api/users/password_reset/request/
        Always returns 200 with the same message, never reveal whether
        the email exists, or attackers can enumerate registered accounts.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email'].lower()

        user = User.objects.filter(email=email, is_active=True).first()
        if user:
            raw_token = PasswordResetToken.issue(user)
            reset_url = f"{settings.BACKEND_URL}/api/users/reset-password/?token={raw_token}"
            send_password_reset_email(user, reset_url)

        return Response({
            "message": "If an account exists for that email, a reset link has been sent."
        })

    @action(detail=False, methods=['post'], url_path='password_reset/confirm')
    def confirm_password_reset(self, request):
        """POST /api/users/password_reset/confirm/"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": "Password reset successfully. Please log in."})

    # ========== SELF-SERVICE: REQUEST DELETION ==========
    @action(detail=False, methods=['post'])
    def request_account_deletion(self, request):
        """
        POST /api/users/request_account_deletion/
        Body: {"password": "...", "reason": "..."}  (reason optional)
        """
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            'message': 'Account scheduled for deletion.',
            'deletionScheduledFor': user.deletion_scheduled_for,
        })

    # ========== ADMIN: SUSPEND / SCHEDULE DELETION / RESTORE ==========
    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAdminUser])
    def set_status(self, request, pk=None):
        """
        POST /api/users/{id}/set_status/
        Body: {"status": "suspended" | "pending_deletion" | "active", "reason": "..."}
        Backs the admin dashboard's status control. Reason is mandatory here,
        this is an operator overriding someone's account, it always needs a paper trail.
        """
        target_user = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = set_account_status(
            user=target_user,
            new_status=serializer.validated_data['status'],
            reason=serializer.validated_data['reason'],
            actor=request.user,
        )
        return Response(UserSerializer(updated).data)

    @action(detail=True, methods=['get'], permission_classes=[permissions.IsAdminUser])
    def status_audit(self, request, pk=None):
        """GET /api/users/{id}/status_audit/, the account's status history."""
        rows = AccountStatusAudit.objects.filter(user_id=pk)
        return Response(AccountStatusAuditSerializer(rows, many=True).data)

    # ========== UPDATE NOTIFICATIONS ==========
    @action(detail=False, methods=['patch'])
    def update_notifications(self, request):
        """PATCH /api/users/update_notifications/"""
        serializer = self.get_serializer(
            request.user,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({
            'message': 'Notification preferences updated',
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    # ========== GET CURRENT USER ==========
    @action(detail=False, methods=['get'])
    def me(self, request):
        """GET /api/users/me/"""
        serializer = UserWithBusinessSerializer(request.user)
        return Response(serializer.data)

    # ========== LOGOUT ==========
    @action(detail=False, methods=['post'])
    def logout(self, request):
        """
        POST /api/users/logout/
        AllowAny on purpose: the access token may already be expired when
        the user logs out, and the web client has no Authorization header
        to send. The refresh token comes from the httpOnly cookie (web) or
        the request body (mobile).
        """
        raw_refresh = request.COOKIES.get(REFRESH_COOKIE_NAME) or request.data.get('refresh')
        if raw_refresh:
            try:
                RefreshToken(raw_refresh).blacklist()
            except TokenError:
                pass  # already expired or already blacklisted

        response = Response({'message': 'Logout successful'}, status=status.HTTP_200_OK)
        clear_refresh_cookie(response)
        return response

    # ========== ADMIN DASHBOARD STATS ==========
    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAdminUser])
    def admin_dashboard_stats(self, request):
        """GET /api/users/admin_dashboard_stats/"""
        stats_data = {
            "total_users": User.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "verified_users": User.objects.filter(email_verified_at__isnull=False).count(),
            "new_users_today": User.objects.filter(date_joined__date=timezone.now().date()).count(),
        }
        serializer = self.get_serializer(stats_data)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def cancel_account_deletion(self, request):
        """
        POST /api/users/cancel_account_deletion/
        AllowAny, the account is deactivated, so the user can't reach this
        through the normal authenticated flow. Logs them straight back in
        on success so they don't need a second round trip.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        return issue_session(request, user, {
            'message': 'Account reactivated.',
            'user': UserSerializer(user).data,
        })


class PasswordResetPageView(TemplateView):
    template_name = "accounts/reset_password.html"


class SessionLimitedTokenRefreshView(TokenRefreshView):
    """
    POST /api/users/token/refresh/
    Web (X-Client: web): refresh token is read from the httpOnly cookie, the
    rotated one is written back to the cookie, and the body carries only the
    new access token plus sessionExpiresAt.
    Mobile: refresh token in the body, rotated one returned in the body.
    A staff member who is over the plan's staff cap is refused before the
    token rotates.
    """
    serializer_class = SessionLimitedTokenRefreshSerializer

    def post(self, request, *args, **kwargs):
        web = is_web_client(request)
        raw_refresh = (
            request.COOKIES.get(REFRESH_COOKIE_NAME) if web else request.data.get('refresh')
        )
        if not raw_refresh:
            return self._reject(web, 'No refresh token provided.')

        if self._staff_seat_blocked(raw_refresh):
            response = Response(
                {'error': STAFF_BLOCKED_MESSAGE, 'code': 'staff_over_plan_limit'},
                status=status.HTTP_403_FORBIDDEN,
            )
            if web:
                clear_refresh_cookie(response)
            return response

        serializer = self.get_serializer(data={'refresh': raw_refresh})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, DRFValidationError):
            return self._reject(web, 'Session expired. Please log in again.')

        validated = serializer.validated_data
        new_refresh = RefreshToken(validated.get('refresh', raw_refresh))
        return build_session_response(
            request,
            access=validated['access'],
            refresh=new_refresh,
        )

    @staticmethod
    def _staff_seat_blocked(raw_refresh) -> bool:
        try:
            user_id = RefreshToken(raw_refresh)['user_id']
        except TokenError:
            return False  # the normal path below turns this into a 401
        user = User.objects.filter(id=user_id).first()
        return bool(user and staff_access_blocked(user))

    @staticmethod
    def _reject(web, detail):
        response = Response({'detail': detail}, status=status.HTTP_401_UNAUTHORIZED)
        if web:
            clear_refresh_cookie(response)  # stop the browser resending a dead token
        return response


GOOGLE_CLIENT_IDS = {
    os.getenv("GOOGLE_IOS_CLIENT_ID"),
    os.getenv("GOOGLE_ANDROID_CLIENT_ID"),
    os.getenv("GOOGLE_WEB_CLIENT_ID"),
}


class GoogleIdTokenLoginView(APIView):
    """
    POST /api/users/google_login/
    Body: { "idToken": "..." }

    Mobile only. The native Google SDK hands the app an id token, which is
    verified here against the iOS, Android and web client ids. Staff accounts
    are refused by login_gate_response and must use email and password.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        signup_token = request.data.get("signup_token")
        raw_token = request.data.get("id_token")
        accepted_terms = request.data.get("accepted_terms") is True

        if not signup_token and not raw_token:
            return Response({"error": "idToken is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user_info = (
                identity_from_signup_token(signup_token) if signup_token
                else verify_mobile_id_token(raw_token)
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

        first_name = user_info.get("given_name", "")
        last_name = user_info.get("family_name", "")
        if not is_new and (user.first_name != first_name or user.last_name != last_name):
            user.first_name = first_name
            user.last_name = last_name
            user.save(update_fields=["first_name", "last_name"])

        return issue_session(request, user, {
            "user": UserSerializer(user).data,
            "business": BusinessSerializer(business).data if business else None,
            "requires_password_change": user.requires_password_change,
            "isNew": is_new,
            "message": "Google login successful",
        })
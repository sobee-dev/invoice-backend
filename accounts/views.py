from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.utils import timezone

from business.models import Business
from business.serializers import BusinessSerializer
from django.conf import settings
from django.contrib.auth import login
from receipts.models import Receipt
from receipts.serializers import ReceiptListSerializer
from .models import User
from .serializers import (
    UserSerializer,
    UserRegistrationSerializer,
    UserUpdateSerializer,
    ChangePasswordSerializer,
    UserListSerializer,
    UserWithBusinessSerializer,AdminDashboardSerializer
)

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
        
        return UserSerializer
        
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['register', 'login']:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]
    
    def get_queryset(self):
        """Filter queryset: Admins see all, users see only themselves"""
        if self.request.user.is_authenticated:
            if self.request.user.is_staff:
                return User.objects.all()
            return User.objects.filter(id=self.request.user.id)
        return User.objects.none()

    @action(detail=False, methods=['post'])
    def register(self, request):
        """POST /api/users/register/"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        response = Response({
        'user': UserSerializer(user).data,
        'message': 'User registered successfully'
    },  status=status.HTTP_201_CREATED)

    # 3. Set the cookie on the response
        response.set_cookie(
            key=settings.SIMPLE_JWT['AUTH_COOKIE'],
            value=access_token,
            httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
        )
        
        return response
    
    @action(detail=False, methods=['post'])
    def login(self, request):
        """POST /api/users/login/"""
        email = request.data.get('email')
        password = request.data.get('password')
        
        if not email or not password:
            return Response({'error': 'Please provide both email and password'}, status=status.HTTP_400_BAD_REQUEST)
        
        user = authenticate(request, email=email.lower(), password=password)
        
        if user is None:
            return Response(
                {'error': 'Invalid credentials'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        if not user.is_active:
            return Response(
                {'error': 'Account is disabled'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)
        
        business = Business.objects.filter(owner=user).first()
        receipts_qs = Receipt.objects.filter(business=business).order_by('-updated_at')[:50] if business else []
        receipts_data = ReceiptListSerializer(receipts_qs, many=True).data
        
        # 2. Prepare Response
        response = Response({
            'user': UserSerializer(user).data,
            'business': BusinessSerializer(business).data if business else None,
            'receipts': receipts_data,
            'message': 'Login successful',
            'access': access_token,
            'refresh': refresh_token,
        
        }, status=status.HTTP_200_OK)
        
        
       # 3. Set Cookie (Matches your register logic)
       
       
        # response.set_cookie(
        #     key=settings.SIMPLE_JWT['AUTH_COOKIE'],
        #     value=access_token,
        #     httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
        #     secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
        #     samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
        # )
        
        return response
        
        
    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAdminUser])
    def admin_dashboard_stats(self, request):
    
        stats_data= {
            "total_users": User.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "verified_users": User.objects.filter(email_verified_at__isnull=False).count(),
            "new_users_today": User.objects.filter(date_joined__date=timezone.now().date()).count(),
        }   
        serializer = self.get_serializer(stats_data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def logout(self, request):
        """POST /api/users/logout/"""
        response = Response({'message': 'Logout successful'}, status=status.HTTP_200_OK)
        
        # Delete the cookie from the browser
        response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
        
        # Optional: Blacklist refresh token if provided
        try:
            refresh_token = request.data.get('refresh')
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
        except Exception:
            pass # Token might already be expired
            
        return response
    
    
    @action(detail=False, methods=['get'])
    def me(self, request):
        """GET /api/users/me/"""
        serializer = UserWithBusinessSerializer(request.user)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def change_password(self, request):
        """POST /api/users/change_password/"""
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'message': 'Password changed successfully'}, status=status.HTTP_200_OK)
    
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
    
    @action(detail=False, methods=['patch'])
    def update_notifications(self, request):
        """PATCH /api/users/update_notifications/"""
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response({
            'message': 'Notification preferences updated',
            'data': serializer.data
        }, status=status.HTTP_200_OK)
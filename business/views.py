from decimal import Decimal

from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action, api_view, permission_classes

from django.shortcuts import get_object_or_404
from django.db.models import Sum
from django.utils import timezone
from billing.models import Subscription
from billing.permissions import HasActiveSubscription
from business.services import get_full_summary_data
from staff.models import StaffMember
from .models import Business, SyncStatus
from .serializers import (
    BusinessSerializer,
    BusinessListSerializer,
    BusinessCreateSerializer,
    BusinessUpdateSerializer,
    BusinessSyncSerializer,
    BusinessOnboardingSerializer
)

# cloudinary uploads
import re
import time
import hashlib
import requests
from django.conf import settings

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from business.utils import get_user_business

ALLOWED_UPLOAD_FOLDERS = {'business-logos', 'business-signatures', 'product-images'}

TRIAL_DAYS = 15

def _build_public_id(folder, business_id, resource_id=None):
    """
    The ONE place that decides an asset's Cloudinary public_id. Used for
    both signing an upload and signing a destroy, so the two operations
    can never disagree about what an asset is called. We always include
    the folder ourselves in the id rather than relying on Cloudinary's
    implicit folder-prepending, which behaves differently depending on
    whether the product environment is in Dynamic or Fixed folder mode —
    this way it doesn't matter which mode the account is in.

    business-logos / business-signatures: one slot per business.
    product-images: one slot per (business, product) pair.
    """
    if resource_id:
        return f"{folder}/{business_id}_{resource_id}"
    return f"{folder}/{business_id}"


def _resolve_target(request, folder, resource_id=None):
    """
    Returns (business, public_id) this request is allowed to write to /
    remove, or (None, None) if the request isn't authorized for that
    target. Product images always require an existing resource_id now —
    uploads for not-yet-created products are staged locally on the client
    and only sent to Cloudinary once the product (and its id) exists, so
    there's no more "pending" namespace or cleanup job to maintain.
    """
    business = get_user_business(request.user)
    if business is None:
        return None, None

    if folder == 'product-images':
        if not resource_id:
            return None, None
        from products.models import Product
        if not Product.objects.filter(id=resource_id, business=business).exists():
            return None, None
        return business, _build_public_id(folder, business.id, resource_id)

    # business-logos / business-signatures — owner-only, one slot per business
    if request.user.role != 'owner':
        return None, None
    return business, _build_public_id(folder, business.id)


def _sign(params):
    to_sign = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    return hashlib.sha1((to_sign + settings.CLOUDINARY_API_SECRET).encode()).hexdigest()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cloudinary_signature(request):
    folder = request.data.get('folder')
    if folder not in ALLOWED_UPLOAD_FOLDERS:
        return Response({'detail': 'Invalid folder.'}, status=400)

    business, public_id = _resolve_target(
        request, folder, resource_id=request.data.get('resource_id'),
    )
    if public_id is None:
        return Response({'detail': 'Invalid or unauthorized target for this upload.'}, status=403)

    timestamp = int(time.time())
    params_to_sign = {'timestamp': timestamp, 'public_id': public_id, 'overwrite': 'true'}
    signature = _sign(params_to_sign)

    return Response({
        'signature':  signature,
        'timestamp':  timestamp,
        'api_key':    settings.CLOUDINARY_API_KEY,
        'cloud_name': settings.CLOUDINARY_CLOUD_NAME,
        'public_id':  public_id,
        'overwrite':  True,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cloudinary_remove(request):
    folder = request.data.get('folder')
    if folder not in ALLOWED_UPLOAD_FOLDERS:
        return Response({'detail': 'Invalid folder.'}, status=400)

    product = None
    if folder == 'product-images':
        from products.models import Product
        business = get_user_business(request.user)
        if business is None:
            return Response({'detail': 'No business associated with this account.'}, status=400)
        resource_id = request.data.get('resource_id')
        try:
            product = Product.objects.get(id=resource_id, business=business)
        except (Product.DoesNotExist, ValueError, TypeError):
            return Response({'detail': 'Product not found.'}, status=404)
        business, public_id = _resolve_target(request, folder, resource_id=product.id)
    else:
        if request.user.role != 'owner':
            return Response({'detail': 'Only business owners can remove this asset.'}, status=403)
        business, public_id = _resolve_target(request, folder)

    if public_id is None:
        return Response({'detail': 'Invalid or unauthorized target.'}, status=403)

    timestamp = int(time.time())
    params_to_sign = {'timestamp': timestamp, 'public_id': public_id}
    signature = _sign(params_to_sign)

    resp = requests.post(
        f'https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/destroy',
        data={
            'public_id': public_id, 'timestamp': timestamp,
            'api_key': settings.CLOUDINARY_API_KEY, 'signature': signature,
        },
        timeout=10,
    )
    result = resp.json()
    if result.get('result') not in ('ok', 'not found'):
        return Response({'detail': 'Could not remove asset from storage.'}, status=502)

    if folder == 'product-images':
        product.image_url = ''
        product.save(update_fields=['image_url', 'updated_at'])
    elif folder == 'business-logos':
        business.logo_url = ''
        business.save(update_fields=['logo_url'])
    else:
        business.signature_url = ''
        business.signature_type = 'none'
        business.signature_text = ''
        business.save(update_fields=['signature_url', 'signature_type', 'signature_text'])

    return Response({'message': 'Removed.'})

class BusinessViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Business management with custom actions for 
    onboarding, synchronization, and profile management.
    """
    queryset = Business.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    
    def get_permissions(self):
        # 'create' and the POST branch of 'me' both run before any Business
        # row exists for this user. HasActiveSubscription needs an existing
        # business to find a subscription on — gating either would permanently
        # block onboarding for a brand-new signup. Only PATCH on 'me' (editing
        # an existing business) is gated.
        if self.action == 'manage_my_business' and self.request.method == 'PATCH':
            return [permissions.IsAuthenticated(), HasActiveSubscription()]
        return [permissions.IsAuthenticated()]

    def get_serializer_class(self):
        """
        Dynamically assign serializers based on the action being performed.
        """
        if self.action == 'list':
            return BusinessListSerializer
        elif self.action == 'create':
            return BusinessCreateSerializer
        elif self.action in ['update', 'partial_update','manage_my_business']:
            return BusinessUpdateSerializer
        elif self.action == 'sync':
            return BusinessSyncSerializer
        elif self.action == 'complete_onboarding':
            return BusinessOnboardingSerializer
        return BusinessSerializer

    def get_queryset(self):
        """
        Admins see all businesses; regular users see only their own.
        """
        queryset = super().get_queryset()
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(owner=self.request.user)
    
    def perform_create(self, serializer):
                business = serializer.save(owner=self.request.user)
                Subscription.objects.get_or_create(
                    business=business,
                    defaults={'trial_ends_at': timezone.now() + timezone.timedelta(days=TRIAL_DAYS)},
                )    

    @action(detail=False, methods=['get', 'patch', 'post'], url_path='me')
    def manage_my_business(self, request):
        user_business = self.get_queryset().first()

        if not user_business and request.user.role == 'staff':
            staff_profile = StaffMember.objects.filter(
                user=request.user, status='active'
            ).select_related('business').first()
            if staff_profile:
                user_business = staff_profile.business

        if request.method == 'GET':
            if not user_business:
                return Response({"detail": "No business found."}, status=status.HTTP_404_NOT_FOUND)
            serializer = BusinessSerializer(user_business)
            return Response(serializer.data)

        if request.method == 'POST':
            if user_business:
                return Response({"detail": "Business already exists."}, status=status.HTTP_400_BAD_REQUEST)
            serializer = BusinessCreateSerializer(data=request.data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            business = serializer.save(owner=request.user)
            Subscription.objects.get_or_create(
                business=business,
                defaults={'trial_ends_at': timezone.now() + timezone.timedelta(days=TRIAL_DAYS)},
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        if request.method == 'PATCH':
            if not user_business:
                return Response({"detail": "Business not found."}, status=status.HTTP_404_NOT_FOUND)
            if request.user.role != 'owner':
                return Response(
                    {"detail": "Only business owners can update business details."},
                    status=status.HTTP_403_FORBIDDEN
                )
            serializer = self.get_serializer(user_business, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
        
            
    @action(detail=True, methods=['post'], url_path='sync')
    def sync(self, request, pk=None):
        """
        POST /api/businesses/{uuid}/sync/
        Update the server_id and sync_status after a successful external sync.
        """
        business = self.get_object()
        serializer = self.get_serializer(business, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='complete-onboarding')
    def complete_onboarding(self, request, pk=None):
        """
        POST /api/businesses/{uuid}/complete-onboarding/
        Final check before allowing the user to start creating receipts.
        """
        business = self.get_object()
        # Force the onboarding_complete flag to True in the request data
        data = request.data.copy()
        data['onboarding_complete'] = True
        
        serializer = self.get_serializer(business, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({
            "status": "Onboarding successful",
            "business": BusinessSerializer(business).data
        })
        
        
    @action(detail=False, methods=['get'], url_path='summary-data')
    def summary_data(self, request):
       
        business = get_object_or_404(self.get_queryset())
        
        # Already built out all the responses in business.services
        data = get_full_summary_data(business)
        return Response(data)
        

    @action(detail=False, methods=['get'])
    def sync_health(self, request):
        """Moved from DashboardViewSet"""
        business = get_object_or_404(self.get_queryset)
        return Response({
            "status": business.sync_status,
            "last_synced": business.updated_at,
            "server_id": business.server_id
        })
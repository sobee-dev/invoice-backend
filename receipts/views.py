from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db import transaction
from rest_framework.pagination import CursorPagination
from django.http import FileResponse

from business.models import Business
from .utils import *

from .models import Receipt, SyncStatus
from .serializers import (
    ReceiptDetailSerializer, 
    ReceiptListSerializer
)

class ReceiptCursorPagination(CursorPagination):
    page_size = 20
    ordering = '-created_at'  # Newest receipts first
    cursor_query_param = 'cursor'
    
    

class ReceiptViewSet(viewsets.ModelViewSet):
    """
    Handles Receipt lifecycle with support for offline-first synchronization.
    """
    queryset = Receipt.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = ReceiptCursorPagination # Enable pagination

    def get_serializer_class(self):
        if self.action == 'list':
            return ReceiptListSerializer
        return ReceiptDetailSerializer

    def get_queryset(self):
        """
        Filter by business ownership and exclude soft-deleted items 
        unless explicitly requested.
        """
        user = self.request.user
        # Only show receipts belonging to businesses the user owns
        queryset = Receipt.objects.filter(
            business__owner=user, 
            deleted_at__is_null=True
        ).select_related('business')
        return queryset
        # Default: hide soft-deleted receipts
       

    @transaction.atomic
    def perform_create(self, serializer):
        """
        Standard creation (online mode).
        """
        serializer.save(sync_status=SyncStatus.SYNCED)

    @action(detail=False, methods=['post'], url_path='bulk-sync')
    def bulk_sync(self, request):
        """
        POST /api/receipts/bulk-sync/
        Used by the frontend to upload multiple receipts created while offline.
        """
        receipts_data = request.data  # Expecting a list of receipt objects
        if not isinstance(receipts_data, list):
            return Response({"error": "Expected a list of receipts"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        results = {"created": 0,"updated": 0,"errors": [] }

        for data in receipts_data:
            items_data = data.pop('items', [])
            receipt_id = data.get('id')
            try:
                # Check if receipt already exists (it might have been partially synced)
                instance = Receipt.objects.filter(id=receipt_id).first()
                
                if instance:
                    serializer = ReceiptDetailSerializer(instance, data=data, partial=True)
                else:
                    # CREATE new (using the client-side UUID)
                    serializer = ReceiptDetailSerializer(data=data)
                
                if serializer.is_valid():
                    user_business = Business.objects.filter(owner=request.user).first()
                    with transaction.atomic():
                        receipt = serializer.save(sync_status=SyncStatus.SYNCED, business=user_business)
                        
                        # Handle Nested Items: Clear and Re-add to avoid duplicates
                        receipt.items.all().delete() 
                        for item in items_data:
                            # Assuming you have a related_name='items' on your ReceiptItem model
                            receipt.items.create(
                                description=item.get('description'),
                                quantity=item.get('quantity'),
                                unitPrice=item.get('unitPrice'),
                                total=item.get('total')
                            )
                        
                        if instance: results["updated"] += 1 
                        else: results["created"] += 1
                else:
                    results["errors"].append({"id": receipt_id, "errors": serializer.errors})
                    
            except Exception as e:
                results["errors"].append({"id": receipt_id, "error": str(e)})

        return Response(results, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='soft-delete')
    def soft_delete_receipt(self, request, pk=None):
        """
        Mark a receipt as deleted without removing it from the DB.
        This allows the frontend to sync the 'deleted' state.
        """
        receipt = self.get_object()
        receipt.soft_delete()
        return Response({"status": "Receipt marked as deleted"}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='changes')
    def get_changes(self, request):
        """
        GET /api/receipts/changes/?last_sync=ISO_TIMESTAMP
        Allows the frontend to pull only what has changed since the last time it was online.
        """
        last_sync = request.query_params.get('last_sync')
        if not last_sync:
            return Response({"error": "last_sync timestamp is required"}, status=400)
            
        changes = Receipt.objects.filter(
            business__owner=request.user,
            updated_at__gt=last_sync
        )
        serializer = ReceiptDetailSerializer(changes, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], url_path='sync-summary')
    def sync_summary(self, request):
        """
        Returns counts of synced vs pending receipts for the user's dashboard.
        """
        user_receipts = Receipt.objects.filter(business__owner=request.user)
        return Response({
            "total_synced": user_receipts.filter(sync_status=SyncStatus.SYNCED).count(),
            "pending_sync": user_receipts.filter(sync_status=SyncStatus.PENDING).count(),
            "errors": user_receipts.filter(sync_status=SyncStatus.ERROR).count(),
            "last_upload": user_receipts.order_by('-updated_at').values_list('updated_at', flat=True).first()
        })
        
        
    
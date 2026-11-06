# documents/views.py
from django.db import transaction
from django.db.models import Q
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounts.permissions import BusinessActivePermission, StaffWithinPlanPermission
from billing.plans import COUNTED_INVOICE_TYPES, INVOICES, check_limit
from business.models import Business
from payments import services as payment_services
from payments.serializers import MarkPaidSerializer, MarkUnpaidSerializer
from receipt_backend_api.pagination import StandardPagination
from staff.models import StaffMember

from . import services as document_services
from .models import Document
from .serializers import DocumentListSerializer, DocumentSerializer, DocumentUpdateSerializer


class DocumentViewSet(viewsets.ModelViewSet):
    pagination_class = StandardPagination

    def get_permissions(self):
        # A view that sets its own permissions replaces the project default,
        # so the suspended-business and staff-seat checks are listed here.
        return [
            permissions.IsAuthenticated(),
            BusinessActivePermission(),
            StaffWithinPlanPermission(),
        ]

    def get_serializer_class(self):
        if self.action == 'list':
            return DocumentListSerializer
        if self.action in ['update', 'partial_update']:
            return DocumentUpdateSerializer
        return DocumentSerializer

    @transaction.atomic
    def perform_update(self, serializer):
        # Re-read the row under a lock so an edit cannot race a deduction.
        instance = Document.objects.select_for_update().get(pk=serializer.instance.pk)
        if instance.status != Document.Status.DRAFT:
            raise PermissionDenied("Only documents in 'DRAFT' status can be edited.")
        if instance.stock_status in (Document.StockStatus.PARTIAL, Document.StockStatus.DEDUCTED):
            raise PermissionDenied('Stock has already been deducted for this document, so it can no longer be edited.')
        serializer.save()

    def _get_base_queryset(self):
        """Returns the unfiltered base queryset scoped to the requesting user's role."""
        user = self.request.user

        if user.role == 'staff':
            staff = StaffMember.objects.filter(
                user=user, status='active'
            ).select_related('business').first()
            if not staff:
                return Document.objects.none()
            return Document.objects.filter(
                business=staff.business,
                created_by=user,
                deleted_at__isnull=True,
            ).select_related('business', 'customer', 'created_by')

        return Document.objects.filter(
            business__owner=user,
            deleted_at__isnull=True,
        ).select_related('business', 'customer', 'created_by')

    def get_queryset(self):
        queryset = self._get_base_queryset()
        params = self.request.query_params

        # One type, or several separated by commas: ?document_type=sales_invoice,proforma_invoice
        doc_type = params.get('document_type')
        if doc_type:
            types = [t.strip() for t in doc_type.split(',') if t.strip()]
            if types:
                queryset = queryset.filter(document_type__in=types)

        doc_status = params.get('status')
        if doc_status:
            queryset = queryset.filter(status=doc_status)

        stock_status = params.get('stock_status')
        if stock_status:
            queryset = queryset.filter(stock_status=stock_status)

        customer_id = params.get('customer')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        search = (params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(document_number__icontains=search) |
                Q(customer_name__icontains=search) |
                Q(supplier_name__icontains=search)
            )

        # The id is the last key so rows that share a date and creation time
        # keep a stable order from one page to the next.
        return queryset.order_by('-document_date', '-created_at', '-id')

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user

        if user.role == 'staff':
            staff = StaffMember.objects.filter(
                user=user, status='active'
            ).select_related('business').first()
            if not staff:
                raise PermissionDenied('No active staff account found.')
            business = staff.business
        else:
            business = Business.objects.filter(owner=user).first()
            if business is None:
                raise PermissionDenied('Create your business before creating documents.')

        Business.objects.select_for_update().get(pk=business.pk)

        # Sales and proforma invoices count toward the monthly limit, supplier
        # orders do not. The check runs under the lock above and in the same
        # transaction as the create, so the 6th invoice on Free cannot slip in.
        if serializer.validated_data.get('document_type') in COUNTED_INVOICE_TYPES:
            check_limit(business, INVOICES)

        serializer.save(business=business, created_by=user)

    def perform_destroy(self, instance):
        if self.request.user != instance.business.owner:
            raise PermissionDenied('Only the business owner is authorized to delete documents.')
        payment_services.soft_delete_document(document_id=instance.id, business=instance.business)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # Re-fetch so totals, stock_status and items reflect the saved state,
        # and respond with the full read serializer instead of the write one.
        fresh = self.get_object()
        return Response(DocumentSerializer(fresh, context=self.get_serializer_context()).data)

    # Custom actions

    @action(detail=True, methods=['post'], url_path='deliver')
    def deliver(self, request, pk=None):
        doc = self.get_object()
        if doc.status != Document.Status.DRAFT:
            return Response(
                {'error': 'Document cannot be marked delivered from its current status.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        doc.mark_delivered()
        return Response(DocumentSerializer(doc).data)

    @action(detail=True, methods=['get'], url_path='clone')
    def clone(self, request, pk=None):
        """Returns an editable template based on an existing document."""
        original_doc = self.get_object()
        items = original_doc.items.all()

        data = dict(DocumentSerializer(original_doc).data)
        for field in [
            'id', 'document_number', 'status', 'created_at', 'updated_at', 'sync_status',
            'paid_at', 'delivered_at', 'amount_paid', 'returned_credit', 'balance_due',
            'stock_status', 'stock_deducted_at', 'is_delivered', 'deleted_at',
        ]:
            data.pop(field, None)

        data['items'] = [
            {
                'product': item.product.id if item.product else None,
                'description': item.description,
                'quantity': item.quantity,
                'unit_price': item.unit_price,
                'total': item.total,
            } for item in items
        ]
        return Response(data)

    @action(detail=True, methods=['post'], url_path='mark-paid')
    def mark_paid(self, request, pk=None):
        doc = self.get_object()
        body = MarkPaidSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        updated = payment_services.mark_invoice_paid(
            document_id=doc.id, business=doc.business, user=request.user,
            method=body.validated_data['method'], reference=body.validated_data['reference'],
        )
        return Response(DocumentSerializer(updated).data)

    @action(detail=True, methods=['post'], url_path='mark-unpaid')
    def mark_unpaid(self, request, pk=None):
        """
        POST /api/documents/{id}/mark-unpaid/
        Body: {"deduct_stock": true}  (optional)
        Moves a draft sales invoice to unpaid and adds it to the customer's
        outstanding balance. All tracked items must be deducted from stock
        first; deduct_stock=true does that in the same atomic step.
        """
        doc = self.get_object()
        body = MarkUnpaidSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        updated = payment_services.mark_invoice_unpaid(
            document_id=doc.id, business=doc.business, user=request.user,
            deduct_stock=body.validated_data['deduct_stock'],
        )
        return Response(DocumentSerializer(updated).data)

    @action(detail=True, methods=['post'], url_path='soft-delete')
    def soft_delete(self, request, pk=None):
        doc = self.get_object()
        if request.user != doc.business.owner:
            return Response(
                {'error': 'Only the business owner is authorized to delete documents.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        payment_services.soft_delete_document(document_id=doc.id, business=doc.business)
        return Response({'status': 'Document deleted', 'id': str(doc.id)})

    @action(detail=True, methods=['post'], url_path='deduct-inventory')
    def deduct_inventory(self, request, pk=None):
        """
        Deduct stock for a sales invoice. Each item tracks how much has been
        deducted, so the same quantity can never be deducted twice.

        Body (choose one):
          { "subtract_all": true }
          { "items": [{"item_id": "<uuid>", "quantity": 5}] }
        """
        doc = self.get_object()
        if doc.status == Document.Status.DRAFT:
            return Response(
                {'error': 'Stock can only be deducted once the invoice is unpaid or paid.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        updated = document_services.deduct_stock(
            document_id=doc.id, business=doc.business, user=request.user,
            items=request.data.get('items'),
            subtract_all=request.data.get('subtract_all') is True,
        )
        return Response(
            {'status': 'Stock deducted.', 'stock_status': updated.stock_status},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='add-to-inventory')
    @transaction.atomic
    def add_to_inventory(self, request, pk=None):
        """Add all line items to inventory for a purchase invoice."""
        from inventory.models import InventoryTransaction

        doc = self.get_object()
        if doc.document_type != Document.DocumentType.PURCHASE_INVOICE:
            return Response(
                {'error': 'add-to-inventory is only valid for purchase invoices.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tracked = doc.items.filter(product__isnull=False).select_related('product')
        if not tracked.exists():
            return Response(
                {'error': 'No inventory-tracked items on this document.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for item in tracked:
            item.product.quantity_on_hand += item.quantity
            item.product.save(update_fields=['quantity_on_hand', 'updated_at'])
            InventoryTransaction.objects.create(
                business=doc.business,
                product=item.product,
                quantity_change=item.quantity,
                transaction_type=InventoryTransaction.TransactionType.PURCHASE_RECEIVED,
                reference_document_id=doc.id,
                initiated_by=request.user,
            )

        return Response({'status': 'Stock added.'}, status=status.HTTP_200_OK)
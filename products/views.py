from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import CursorPagination
from django.db.models import Q, DecimalField, Max, OuterRef, Subquery, Sum, F, Case, When, Value, BooleanField
from django.db.models.functions import Coalesce
from django.db import transaction
from billing.permissions import HasActiveSubscription
from business.models import Business
from business.utils import get_user_business
from documents.models import Document, DocumentItem
from .models import Product
from .serializers import BulkDeductSerializer, ProductSerializer, ProductListSerializer, ProductUpdateSerializer, StockAdjustmentSerializer
from .services import check_and_notify_stock_level
from rest_framework.exceptions import ValidationError as DRFValidationError
from inventory.models import InventoryTransaction
import re
import time
import hashlib
import requests
from django.conf import settings

_PENDING_TOKEN_PATTERN = re.compile(r'^[a-zA-Z0-9\-]{1,64}$')


def _cloudinary_rename(from_public_id, to_public_id):
    """Moves a pending-token asset to its permanent product-scoped path.
    Returns the Cloudinary response dict; callers check for 'secure_url'
    rather than raising, since a missing pending upload (user never picked
    an image) is a normal, non-error outcome here."""
    timestamp = int(time.time())
    params_to_sign = {
        'from_public_id': from_public_id,
        'to_public_id': to_public_id,
        'overwrite': 'true',
        'timestamp': timestamp,
    }
    to_sign = '&'.join(f'{k}={v}' for k, v in sorted(params_to_sign.items()))
    signature = hashlib.sha1((to_sign + settings.CLOUDINARY_API_SECRET).encode()).hexdigest()
    resp = requests.post(
        f'https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/rename',
        data={
            'from_public_id': from_public_id, 'to_public_id': to_public_id,
            'overwrite': 'true', 'timestamp': timestamp,
            'api_key': settings.CLOUDINARY_API_KEY, 'signature': signature,
        },
        timeout=10,
    )
    return resp.json()

class ProductCursorPagination(CursorPagination):
    page_size = 20
    ordering = 'name'
    cursor_query_param = 'cursor'


class ProductViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = ProductCursorPagination
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    
    WRITE_ACTIONS = {'create', 'update', 'partial_update', 'deactivate', 'adjust_stock', 'bulk_deduct'}

    def get_permissions(self):
        if self.action in self.WRITE_ACTIONS:
            return [permissions.IsAuthenticated(), HasActiveSubscription()]
        return [permissions.IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == 'list':
            return ProductListSerializer
        if self.action in ['update', 'partial_update']:
            return ProductUpdateSerializer
        return ProductSerializer
        

    def get_queryset(self):
        user = self.request.user
        business = get_user_business(user)
        if business is None:
            return Product.objects.none()

        queryset = Product.objects.filter(business=business).select_related('business')
        
        # 2.  Filters
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(sku__icontains=search)
            )


        sold_subquery = DocumentItem.objects.filter(
            product=OuterRef('pk'),
            document__document_type=Document.DocumentType.SALES_INVOICE
        ).values('product').annotate(
            total=Sum('quantity')
        ).values('total')

        #3. Annotations
        return queryset.annotate(
            total_sold_qty=Coalesce(
                Subquery(sold_subquery, output_field=DecimalField(max_digits=10, decimal_places=3)),
                Value(0),
                output_field=DecimalField(max_digits=10, decimal_places=3),
            ),
            available_stock=F('quantity_on_hand') - F('quantity_reserved'),
            low_stock_flag=Case(
                When(
                    quantity_on_hand__lte=F('quantity_reserved') + F('reorder_level'),
                    then=Value(True)
                ),
                default=Value(False),
                output_field=BooleanField()
            )
        )

    def perform_create(self, serializer):
        business = get_user_business(self.request.user)
        if business is None:
            raise DRFValidationError({'detail': 'No business associated with this account.'})
        sku = serializer.validated_data.get('sku')
        if sku and Product.objects.filter(business=business, sku=sku).exists():
            raise DRFValidationError({'sku': 'A product with this SKU already exists.'})
        product = serializer.save(business=business)

        # If the client uploaded an image before the product existed (new-
        # product flow), it's sitting under a pending/ token path — move it
        # to the product's permanent slot now that we have a real ID.
        pending_token = self.request.data.get('pending_image_token')
        if pending_token and _PENDING_TOKEN_PATTERN.match(pending_token):
            from_public_id = f"product-images/pending_{business.id}_{pending_token}"
            to_public_id = f"product-images/{business.id}_{product.id}"
            result = _cloudinary_rename(from_public_id, to_public_id)
            if result.get('secure_url'):
                product.image_url = result['secure_url']
                product.save(update_fields=['image_url', 'updated_at'])
        

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        if request.user.role != 'owner':
            return Response(
                {'error': 'Only business owners can deactivate products.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        product = self.get_object()
        product.is_active = False
        product.save(update_fields=['is_active', 'updated_at'])
        return Response(
            {'status': 'Product deactivated', 'id': str(product.id)},
            status=status.HTTP_200_OK
        )
        
    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        business = get_user_business(request.user)
        
        if business is None:
            return Response({'error': 'No business associated with this account.'}, status=status.HTTP_400_BAD_REQUEST)
        
        
        # Aggregate totals
        stats = Product.objects.filter(business=business).aggregate(
            total_stock=Sum('quantity_on_hand'),
            max_stock=Max('quantity_on_hand')
        )
        
        # Find product with max stock
        highest_stock_product = Product.objects.filter(
            business=business
        ).order_by('-quantity_on_hand').first()
        
        return Response({
            'total_stock_value': stats['total_stock'] or 0,
            'highest_stock_product': highest_stock_product.name if highest_stock_product else None,
            'highest_stock_count': highest_stock_product.quantity_on_hand if highest_stock_product else 0
        })
        
        
    @action(detail=True, methods=['post'], url_path='adjust-stock')
    @transaction.atomic
    def adjust_stock(self, request, pk=None):
        """
        POST /api/products/{id}/adjust-stock/
        Body: { "quantity_change": <decimal>, "reason": "<string>" }

        Applies a manual stock correction to quantity_on_hand and logs
        a matching InventoryTransaction(transaction_type=ADJUSTMENT).
        Positive quantity_change increases stock, negative decreases it.
        """
        product = self.get_object()

        serializer = StockAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        qty_change = serializer.validated_data['quantity_change']
        reason = serializer.validated_data['reason']

        new_quantity = product.quantity_on_hand + qty_change
        if new_quantity < 0:
            return Response(
                {
                    'error': (
                        f"Adjustment would result in negative stock. "
                        f"Current: {product.quantity_on_hand}, Requested change: {qty_change}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        product.quantity_on_hand = new_quantity
        product.save(update_fields=['quantity_on_hand', 'updated_at'])

        tx = InventoryTransaction.objects.create(
            business=product.business,
            product=product,
            quantity_change=qty_change,
            transaction_type=InventoryTransaction.TransactionType.ADJUSTMENT,
            reference_document_id=None,
            initiated_by=request.user,
            reason=reason,
        )

        check_and_notify_stock_level(product)

        return Response(
            {
                'status': 'Stock adjusted.',
                'product': ProductSerializer(product).data,
                'transaction_id': str(tx.id),
            },
            status=status.HTTP_200_OK,
        )    
        
        
    @action(detail=False, methods=['post'], url_path='bulk-deduct')
    @transaction.atomic
    def bulk_deduct(self, request):
        """
        POST /api/products/bulk-deduct/
        Body: {
            "reference_document_id": "<uuid?>",
            "items": [
                { "product_id": "<uuid>", "quantity": <decimal>, "reason": "<string?>" },
                ...
            ]
        }

        Deducts stock for multiple products in one all-or-nothing
        operation, logging each as InventoryTransaction(
        transaction_type=SALES_CONFIRMED) tagged with
        reference_document_id so it's traceable back to the invoice
        that triggered it. All items are validated before anything is
        written — a single insufficient-stock item fails the whole
        batch rather than leaving some products deducted and others not.
        """
        business = get_user_business(request.user)
        if business is None:
            return Response({'error': 'No business associated with this account.'}, status=status.HTTP_400_BAD_REQUEST)
        

        serializer = BulkDeductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reference_document_id = serializer.validated_data.get('reference_document_id')
        items = serializer.validated_data['items']

        product_ids = [item['product_id'] for item in items]
        # select_for_update locks these rows for the transaction so two
        # overlapping bulk-deduct calls on the same product can't both
        # read a stale quantity_on_hand and both succeed.
        products = {
            p.id: p for p in Product.objects.select_for_update().filter(
                id__in=product_ids, business=business
            )
        }

        errors = []
        for item in items:
            product = products.get(item['product_id'])
            if product is None:
                errors.append({'product_id': str(item['product_id']), 'error': 'Product not found.'})
                continue
            if product.quantity_on_hand - item['quantity'] < 0:
                errors.append({
                    'product_id': str(product.id),
                    'error': (
                        f"Insufficient stock. Current: {product.quantity_on_hand}, "
                        f"Requested: {item['quantity']}."
                    ),
                })

        if errors:
            return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)

        results = []
        for item in items:
            product = products[item['product_id']]
            product.quantity_on_hand = product.quantity_on_hand - item['quantity']
            product.save(update_fields=['quantity_on_hand', 'updated_at'])

            tx = InventoryTransaction.objects.create(
                business=business,
                product=product,
                quantity_change=-item['quantity'],
                transaction_type=InventoryTransaction.TransactionType.SALES_CONFIRMED,
                reference_document_id=reference_document_id,
                initiated_by=request.user,
                reason=item.get('reason', ''),
            )

            check_and_notify_stock_level(product)

            results.append({
                'product_id': str(product.id),
                'transaction_id': str(tx.id),
                'new_quantity': str(product.quantity_on_hand),
            })

        return Response({'status': 'Stock deducted.', 'results': results}, status=status.HTTP_200_OK)
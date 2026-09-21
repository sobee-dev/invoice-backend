from decimal import Decimal

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import CursorPagination


from business.models import Business
from business.utils import get_user_business
from .models import Customer
from .serializers import CustomerSerializer, CustomerListSerializer
from django.db.models import Q, Sum, Count, DecimalField, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError as DRFValidationError


class CustomerCursorPagination(CursorPagination):
    page_size = 20
    ordering = 'full_name' 
    cursor_query_param = 'cursor'


class CustomerViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CustomerCursorPagination
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'list':
            return CustomerListSerializer
        return CustomerSerializer

    def get_queryset(self):
        business = get_user_business(self.request.user)
        if business is None:
            return Customer.objects.none()

        queryset = Customer.objects.filter(
            business=business
        ).select_related('business').annotate(
            ltv=Sum('documents__grand_total', filter=Q(documents__status='paid'))
        )

        status_filter = self.request.query_params.get('status')
        if status_filter in ('active', 'inactive'):
            queryset = queryset.filter(status=status_filter)

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(full_name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone__icontains=search)
            )
        return queryset

    def perform_create(self, serializer):
        business = get_user_business(self.request.user)
        if business is None:
            raise DRFValidationError({'detail': 'No business associated with this account.'})
        serializer.save(business=business)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        customer = self.get_object()
        customer.status = 'inactive'
        customer.save(update_fields=['status'])
        return Response(CustomerSerializer(customer).data, status=status.HTTP_200_OK)


    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        customer = self.get_object()
        customer.status = 'active'
        customer.save(update_fields=['status'])
        return Response(CustomerSerializer(customer).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='analytics')
    def analytics(self, request):
        business = get_user_business(request.user)
        if business is None:
            return Response({'error': 'No business associated with this account.'}, status=status.HTTP_400_BAD_REQUEST)
        
        base = Customer.objects.filter(business=business).annotate(
            ltv=Coalesce(
                Sum('documents__grand_total', filter=Q(documents__status='paid')),
                Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )

        total_count = base.count()
        active_count = base.filter(status='active').count()
        inactive_count = total_count - active_count

        totals = base.aggregate(
            total_ltv=Coalesce(Sum('ltv'), Value(Decimal('0.00')), output_field=DecimalField(max_digits=12, decimal_places=2)),
        )
        total_outstanding = Customer.objects.filter(business=business).aggregate(
            total_outstanding=Coalesce(Sum('outstanding_balance'), Value(Decimal('0.00')), output_field=DecimalField(max_digits=12, decimal_places=2)),
        )['total_outstanding']
        avg_ltv = (totals['total_ltv'] / total_count) if total_count > 0 else Decimal('0.00')

        balance_due_qs = base.filter(outstanding_balance__gt=0).order_by('-outstanding_balance')
        balance_due_count = balance_due_qs.count()

        top_clients = base.order_by('-ltv')[:3]

        payment_breakdown = list(
            base.values('payment_method_preference')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        def serialize_customer(c):
            return {
                'id': str(c.id),
                'full_name': c.full_name,
                'email': c.email,
                'phone': c.phone,
                'lifetime_value': c.ltv,
                'outstanding_balance': c.outstanding_balance,
            }

        return Response({
            'total_customers': total_count,
            'active_count': active_count,
            'inactive_count': inactive_count,
            'total_ltv': totals['total_ltv'],
            'avg_ltv': avg_ltv,
            'total_outstanding': total_outstanding,
            'balance_due_count': balance_due_count,
            'top_clients': [serialize_customer(c) for c in top_clients],
            'balance_due_clients': [serialize_customer(c) for c in balance_due_qs[:20]],
            'payment_breakdown': [
                {'preference': row['payment_method_preference'], 'count': row['count']}
                for row in payment_breakdown
            ],
        })
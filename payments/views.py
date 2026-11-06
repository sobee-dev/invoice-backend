# payments/views.py
import uuid

from django.db.models import Count, Prefetch, Sum
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import BusinessActivePermission, StaffWithinPlanPermission
from customers.models import Customer
from documents.exceptions import DomainError
from documents.models import BALANCE_DUE_EXPR, Document, DocumentItem

from . import services
from .models import ItemReturn, Payment, PaymentAllocation
from .permissions import CanManagePayments, IsBusinessOwner
from .serializers import (
    InvoiceAllocationSerializer,
    InvoiceItemSerializer,
    InvoiceSummarySerializer,
    ItemReturnSerializer,
    OutstandingInvoiceSerializer,
    PaymentSerializer,
    RecordPaymentSerializer,
    ReturnItemsSerializer,
    VoidPaymentSerializer,
)


class PaymentWriteThrottle(UserRateThrottle):
    """Extra brake on anything that moves money or stock."""
    scope = 'payments_write'
    rate = '60/min'


class SmallLimitOffsetPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 100


class PaymentsBaseView(APIView):
    """Owner or managerial staff only. A Free plan does not lock payments."""

    def _is_write(self):
        return self.request.method not in SAFE_METHODS

    def get_permissions(self):
        return [
            IsAuthenticated(), BusinessActivePermission(),
            StaffWithinPlanPermission(), CanManagePayments(),
        ]

    def get_throttles(self):
        throttles = super().get_throttles()
        if self._is_write():
            throttles.append(PaymentWriteThrottle())
        return throttles

    @property
    def business(self):
        return self.request._payments_business


def _payments_queryset(business, **filters):
    return (
        Payment.objects.filter(business=business, **filters)
        .select_related('recorded_by')
        .prefetch_related(Prefetch('allocations', queryset=PaymentAllocation.objects.select_related('document')))
        .order_by('-created_at', '-id')
    )


def build_invoice_history(doc):
    items = DocumentItem.objects.filter(document=doc).order_by('order', 'id')
    allocations = (
        PaymentAllocation.objects.filter(document=doc)
        .select_related('payment', 'payment__recorded_by')
        .order_by('-payment__created_at')
    )
    returns = ItemReturn.objects.filter(document=doc).select_related('item').order_by('-created_at')
    return {
        'invoice': InvoiceSummarySerializer(doc).data,
        'items': InvoiceItemSerializer(items, many=True).data,
        'payments': InvoiceAllocationSerializer(allocations, many=True).data,
        'returns': ItemReturnSerializer(returns, many=True).data,
    }


class CustomerPaymentsView(PaymentsBaseView):
    """
    GET  /api/payments/customers/{id}/payments/  payment history plus outstanding summary
    POST /api/payments/customers/{id}/payments/  record a payment
    """

    def get(self, request, customer_id):
        customer = get_object_or_404(Customer, pk=customer_id, business=self.business)
        paginator = SmallLimitOffsetPagination()
        page = paginator.paginate_queryset(
            _payments_queryset(self.business, customer=customer), request, view=self,
        )
        response = paginator.get_paginated_response(PaymentSerializer(page, many=True).data)
        response.data['summary'] = {
            'outstanding_balance': customer.outstanding_balance,
            'unpaid_invoice_count': Document.objects.filter(
                business=self.business, customer=customer,
                document_type=Document.DocumentType.SALES_INVOICE, status=Document.Status.UNPAID,
            ).count(),
        }
        return response

    def post(self, request, customer_id):
        body = RecordPaymentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        d = body.validated_data

        payment, created = services.record_payment(
            business=self.business, customer_id=customer_id, user=request.user,
            amount=d['amount'], method=d['method'], reference=d['reference'], note=d['note'],
            document_id=d['document_id'], idempotency_key=d['idempotency_key'],
        )
        payment = _payments_queryset(self.business).get(pk=payment.pk)
        customer = Customer.objects.get(pk=customer_id, business=self.business)
        return Response(
            {'payment': PaymentSerializer(payment).data, 'outstanding_balance': customer.outstanding_balance},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class InvoicePaymentHistoryView(PaymentsBaseView):
    """GET /api/payments/invoices/{id}/history/"""

    def get(self, request, document_id):
        doc = get_object_or_404(
            Document.objects.select_related('customer'),
            pk=document_id, business=self.business,
            document_type=Document.DocumentType.SALES_INVOICE,
        )
        return Response(build_invoice_history(doc))


class InvoiceReturnsView(PaymentsBaseView):
    """POST /api/payments/invoices/{id}/returns/  body: {"items": [{"item_id", "quantity"}], "reason": ""}"""

    def post(self, request, document_id):
        body = ReturnItemsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        doc = services.return_items(
            document_id=document_id, business=self.business, user=request.user,
            lines=body.validated_data['items'], reason=body.validated_data['reason'],
        )
        doc = Document.objects.select_related('customer').get(pk=doc.pk)
        return Response(build_invoice_history(doc), status=status.HTTP_201_CREATED)


class VoidPaymentView(PaymentsBaseView):
    """POST /api/payments/records/{id}/void/  owner only."""

    def get_permissions(self):
        return super().get_permissions() + [IsBusinessOwner()]

    def post(self, request, payment_id):
        body = VoidPaymentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payment = services.void_payment(
            payment_id=payment_id, business=self.business, user=request.user,
            reason=body.validated_data['reason'],
        )
        payment = _payments_queryset(self.business).get(pk=payment.pk)
        customer = Customer.objects.get(pk=payment.customer_id, business=self.business)
        return Response({'payment': PaymentSerializer(payment).data, 'outstanding_balance': customer.outstanding_balance})


class OutstandingInvoicesView(PaymentsBaseView):
    """GET /api/payments/outstanding/?customer=<uuid>  unpaid invoices with a balance, oldest first."""

    def get(self, request):
        qs = (
            Document.objects.filter(
                business=self.business,
                document_type=Document.DocumentType.SALES_INVOICE,
                status=Document.Status.UNPAID,
            )
            .annotate(outstanding=BALANCE_DUE_EXPR)
            .filter(outstanding__gt=0)
        )
        customer = request.query_params.get('customer')
        if customer:
            try:
                qs = qs.filter(customer_id=uuid.UUID(customer))
            except ValueError:
                raise DomainError('Invalid customer id.', 'invalid_customer')

        totals = qs.aggregate(total=Sum('outstanding'), invoices=Count('id'), customers=Count('customer', distinct=True))

        paginator = SmallLimitOffsetPagination()
        page = paginator.paginate_queryset(qs.order_by('document_date', 'created_at', 'id'), request, view=self)
        response = paginator.get_paginated_response(OutstandingInvoiceSerializer(page, many=True).data)
        response.data['summary'] = {
            'total_outstanding': totals['total'] or 0,
            'invoice_count': totals['invoices'],
            'customer_count': totals['customers'],
        }
        return response
from django.urls import path

from .views import (
    CustomerPaymentsView,
    InvoicePaymentHistoryView,
    InvoiceReturnsView,
    OutstandingInvoicesView,
    VoidPaymentView,
)

urlpatterns = [
    path('customers/<uuid:customer_id>/payments/', CustomerPaymentsView.as_view(), name='customer-payments'),
    path('invoices/<uuid:document_id>/history/', InvoicePaymentHistoryView.as_view(), name='invoice-payment-history'),
    path('invoices/<uuid:document_id>/returns/', InvoiceReturnsView.as_view(), name='invoice-returns'),
    path('records/<uuid:payment_id>/void/', VoidPaymentView.as_view(), name='payment-void'),
    path('outstanding/', OutstandingInvoicesView.as_view(), name='outstanding-invoices'),
]
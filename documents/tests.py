from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from datetime import date
from accounts.models import User
from business.models import Business
from documents.models import Document


def make_user(email, role='owner'):
    return User.objects.create_user(email=email, password='testpass123', role=role)


def make_business(owner):
    return Business.objects.create(
        owner=owner,
        name='Test Business',
        address_one='1 Test St',
        phone='1234567890',
        email='biz@test.com',
        currency='$',
        selected_template_id='default',
        signature_type='none',
    )


def make_document(business, user, doc_type='sales_invoice', number='001'):
    return Document.objects.create(
        business=business,
        created_by=user,
        document_type=doc_type,
        document_number=number,
        document_date=date.today(),
        customer_name='Test Customer',
        subtotal='100.00',
        tax_rate='0.1500',
        tax_amount='15.00',
        discount='0.00',
        grand_total='115.00',
    )


class DocumentModelTest(TestCase):

    def setUp(self):
        self.owner = make_user('owner@test.com')
        self.business = make_business(self.owner)

    def test_create_sales_invoice_document(self):
        doc = make_document(self.business, self.owner, doc_type='sales_invoice', number='SI-001')
        self.assertEqual(doc.document_type, Document.DocumentType.SALES_INVOICE)
        self.assertEqual(doc.status, Document.Status.DRAFT)
        self.assertEqual(str(doc), 'Sales Invoice #SI-001')

    def test_filter_by_document_type(self):
        make_document(self.business, self.owner, doc_type='sales_invoice', number='SI-001')
        make_document(self.business, self.owner, doc_type='purchase_invoice', number='PI-001')
        make_document(self.business, self.owner, doc_type='proforma_invoice', number='PRO-001')

        sales = Document.objects.filter(document_type=Document.DocumentType.SALES_INVOICE)
        purchases = Document.objects.filter(document_type=Document.DocumentType.PURCHASE_INVOICE)

        self.assertEqual(sales.count(), 1)
        self.assertEqual(purchases.count(), 1)

    def test_filter_by_status(self):
        doc = make_document(self.business, self.owner, number='D-001')
        self.assertEqual(
            Document.objects.filter(status=Document.Status.DRAFT).count(), 1
        )

        doc.status = Document.Status.PAID
        doc.save(update_fields=['status', 'updated_at'])

        self.assertEqual(
            Document.objects.filter(status=Document.Status.PAID).count(), 1
        )
        self.assertEqual(
            Document.objects.filter(status=Document.Status.DRAFT).count(), 0
        )


class DocumentCreatedByTest(TestCase):

    def setUp(self):
        self.owner = make_user('owner@test.com')
        self.business = make_business(self.owner)
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_post_sets_created_by_to_authenticated_user(self):
        payload = {
            'documentType': 'sales_invoice',
            'documentNumber': 'SI-100',
            'documentDate': str(date.today()),
            'customerName': 'Jane Doe',
            'subtotal': '50.00',
            'taxRate': '0.1500',
            'taxAmount': '7.50',
            'discount': '0.00',
            'grandTotal': '57.50',
        }
        response = self.client.post('/api/documents/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # response.data is the raw dict (pre-render), so keys are snake_case
        self.assertEqual(response.data['created_by']['id'], str(self.owner.id))
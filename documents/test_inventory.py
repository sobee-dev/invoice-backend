"""
Integration tests for manual inventory actions.

Covers:
  - POST /api/documents/{id}/deduct-inventory/  (sales invoices)
  - POST /api/documents/{id}/add-to-inventory/  (purchase invoices)
  - POST /api/documents/{id}/deliver/           (draft -> delivered, status-only)
  - Document.mark_paid() / Document.soft_delete() — status-only, no inventory side-effects
  - Access control (unauthenticated, wrong document type, staff scoping)

Note: there is no confirm()/cancel() lifecycle or CONFIRMED/CANCELLED status on
Document — deduct-inventory and add-to-inventory work directly off whatever
status the document is currently in (typically DRAFT), gated only by
document_type. This file was rewritten to match that actual behavior.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from business.models import Business
from staff.models import StaffMember
from documents.models import Document, DocumentItem
from inventory.models import InventoryTransaction
from products.models import Product


# ─────────────────────────── shared helpers ───────────────────────────────────

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


def make_product(business, qty_on_hand=Decimal('100.000'), qty_reserved=Decimal('0.000')):
    count = Product.objects.count()
    return Product.objects.create(
        business=business,
        name=f'Product {count}',
        sku=f'SKU-{count}',
        unit_price=Decimal('10.00'),
        quantity_on_hand=qty_on_hand,
        quantity_reserved=qty_reserved,
    )


def make_document(business, doc_type, owner=None, number=None):
    count = Document.objects.count()
    return Document.objects.create(
        business=business,
        created_by=owner,
        document_type=doc_type,
        document_number=number or f'DOC-{count + 1}',
        document_date=date.today(),
        customer_name='Test Customer',
        grand_total=Decimal('0.00'),
    )


def add_item(document, product, quantity):
    qty = Decimal(str(quantity))
    return DocumentItem.objects.create(
        document=document,
        product=product,
        description=product.name,
        quantity=qty,
        unit_price=product.unit_price,
        total=qty * product.unit_price,
    )


# ─────────────── Model-level lifecycle tests (TestCase) ───────────────────────

class MarkPaidDoesNotTouchInventoryTest(TestCase):
    """mark_paid() only updates status/paid_at — no inventory side-effects."""

    def setUp(self):
        self.owner = make_user('owner@test.com')
        self.business = make_business(self.owner)
        self.product = make_product(self.business, qty_on_hand=Decimal('50.000'))
        self.doc = make_document(
            self.business, Document.DocumentType.SALES_INVOICE, owner=self.owner
        )
        add_item(self.doc, self.product, 10)

    def test_mark_paid_leaves_inventory_unchanged(self):
        self.doc.mark_paid()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('50.000'))
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_mark_paid_changes_status_and_sets_paid_at(self):
        self.doc.mark_paid()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.PAID)
        self.assertIsNotNone(self.doc.paid_at)


class DeliverDoesNotTouchInventoryTest(TestCase):
    """mark_delivered() only updates status/delivery fields — no inventory side-effects."""

    def setUp(self):
        self.owner = make_user('owner@test.com')
        self.business = make_business(self.owner)
        self.product = make_product(self.business, qty_on_hand=Decimal('50.000'))
        self.doc = make_document(
            self.business, Document.DocumentType.SALES_INVOICE, owner=self.owner
        )
        add_item(self.doc, self.product, 10)
        # Document starts in DRAFT by default — no precondition step needed
        # before calling mark_delivered() directly at the model level.

    def test_deliver_leaves_inventory_unchanged(self):
        self.doc.mark_delivered()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('50.000'))
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_deliver_marks_document_delivered(self):
        self.doc.mark_delivered()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DELIVERED)
        self.assertTrue(self.doc.is_delivered)


class SoftDeleteDoesNotTouchInventoryTest(TestCase):
    """soft_delete() only sets deleted_at/status — no inventory side-effects."""

    def setUp(self):
        self.owner = make_user('owner@test.com')
        self.business = make_business(self.owner)
        self.product = make_product(self.business, qty_on_hand=Decimal('50.000'))
        self.doc = make_document(
            self.business, Document.DocumentType.SALES_INVOICE, owner=self.owner
        )
        add_item(self.doc, self.product, 15)

    def test_soft_delete_leaves_inventory_unchanged(self):
        self.doc.soft_delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('50.000'))
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_soft_delete_changes_status_and_sets_deleted_at(self):
        self.doc.soft_delete()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DELETED)
        self.assertIsNotNone(self.doc.deleted_at)

    def test_soft_deleted_document_excluded_from_default_manager(self):
        self.doc.soft_delete()
        # Document.objects uses DocumentManager, which filters deleted_at__isnull=True
        self.assertFalse(Document.objects.filter(id=self.doc.id).exists())
        self.assertTrue(Document.all_objects.filter(id=self.doc.id).exists())


# ─────────────────── API tests (APITestCase) ──────────────────────────────────

class DeductInventoryEndpointTest(APITestCase):
    """POST /api/documents/{id}/deduct-inventory/"""

    def setUp(self):
        self.owner = make_user('owner@test.com', role='owner')
        self.business = make_business(self.owner)
        self.product = make_product(self.business, qty_on_hand=Decimal('100.000'))
        self.doc = make_document(
            self.business, Document.DocumentType.SALES_INVOICE, owner=self.owner
        )
        self.item = add_item(self.doc, self.product, 20)
        # No status precondition — deduct-inventory works directly off the
        # document's document_type, regardless of status (draft by default here).
        self.client.force_authenticate(user=self.owner)

    def _url(self, doc_id=None):
        return f'/api/documents/{doc_id or self.doc.id}/deduct-inventory/'

    def test_subtract_all_deducts_full_quantity(self):
        response = self.client.post(self._url(), {'subtract_all': True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('80.000'))

    def test_subtract_all_creates_transaction_record(self):
        self.client.post(self._url(), {'subtract_all': True}, format='json')
        tx = InventoryTransaction.objects.get(reference_document_id=self.doc.id)
        self.assertEqual(tx.transaction_type, InventoryTransaction.TransactionType.SALES_CONFIRMED)
        self.assertEqual(tx.quantity_change, Decimal('-20.000'))

    def test_partial_deduction_by_item(self):
        response = self.client.post(self._url(), {
            'items': [{'item_id': str(self.item.id), 'quantity': 7}]
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('93.000'))

    def test_insufficient_stock_returns_400(self):
        # Set stock below what the item requires
        self.product.quantity_on_hand = Decimal('5.000')
        self.product.save(update_fields=['quantity_on_hand', 'updated_at'])

        response = self.client.post(self._url(), {'subtract_all': True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Stock must remain untouched
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('5.000'))

    def test_missing_body_returns_400(self):
        response = self.client.post(self._url(), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_returns_401(self):
        self.client.logout()
        response = self.client.post(self._url(), {'subtract_all': True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_wrong_document_type_returns_400(self):
        purchase_doc = make_document(
            self.business, Document.DocumentType.PURCHASE_INVOICE,
            owner=self.owner, number='PO-001',
        )
        response = self.client.post(self._url(purchase_doc.id), {'subtract_all': True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_item_id_returns_404(self):
        import uuid
        response = self.client.post(self._url(), {
            'items': [{'item_id': str(uuid.uuid4()), 'quantity': 5}]
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AddToInventoryEndpointTest(APITestCase):
    """POST /api/documents/{id}/add-to-inventory/"""

    def setUp(self):
        self.owner = make_user('owner@test.com', role='owner')
        self.business = make_business(self.owner)
        self.product = make_product(self.business, qty_on_hand=Decimal('10.000'))
        self.doc = make_document(
            self.business, Document.DocumentType.PURCHASE_INVOICE, owner=self.owner
        )
        add_item(self.doc, self.product, 25)
        # No status precondition here either.
        self.client.force_authenticate(user=self.owner)

    def _url(self, doc_id=None):
        return f'/api/documents/{doc_id or self.doc.id}/add-to-inventory/'

    def test_adds_all_items_to_stock(self):
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity_on_hand, Decimal('35.000'))

    def test_creates_purchase_received_transaction(self):
        self.client.post(self._url())
        tx = InventoryTransaction.objects.get(reference_document_id=self.doc.id)
        self.assertEqual(tx.transaction_type, InventoryTransaction.TransactionType.PURCHASE_RECEIVED)
        self.assertEqual(tx.quantity_change, Decimal('25.000'))

    def test_unauthenticated_returns_401(self):
        self.client.logout()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_wrong_document_type_returns_400(self):
        sales_doc = make_document(
            self.business, Document.DocumentType.SALES_INVOICE,
            owner=self.owner, number='SI-001',
        )
        response = self.client.post(self._url(sales_doc.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_tracked_items_returns_400(self):
        empty_doc = make_document(
            self.business, Document.DocumentType.PURCHASE_INVOICE,
            owner=self.owner, number='PO-EMPTY',
        )
        response = self.client.post(self._url(empty_doc.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class DeliverEndpointTest(APITestCase):
    """POST /api/documents/{id}/deliver/ — status transition, DRAFT-only precondition."""

    def setUp(self):
        self.owner = make_user('owner@test.com', role='owner')
        self.business = make_business(self.owner)
        self.doc = make_document(
            self.business, Document.DocumentType.SALES_INVOICE, owner=self.owner
        )

    def _url(self, doc_id=None):
        return f'/api/documents/{doc_id or self.doc.id}/deliver/'

    def test_authenticated_owner_deliver_returns_200(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DELIVERED)
        self.assertTrue(self.doc.is_delivered)

    def test_deliver_from_non_draft_returns_400(self):
        self.doc.mark_paid()  # moves status away from DRAFT
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_returns_401(self):
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_cannot_deliver_another_staffs_document(self):
        staff_a = make_user('staff_a@test.com', role='staff')
        staff_b = make_user('staff_b@test.com', role='staff')
        StaffMember.objects.create(user=staff_a, business=self.business, status='active')
        StaffMember.objects.create(user=staff_b, business=self.business, status='active')

        doc_b = make_document(
            self.business, Document.DocumentType.SALES_INVOICE,
            owner=staff_b, number='STAFF-B-001',
        )

        self.client.force_authenticate(user=staff_a)
        response = self.client.post(self._url(doc_b.id))
        # staff queryset is scoped to created_by=request.user, so doc_b is
        # invisible to staff_a -> get_object() 404s before any status check.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        doc_b.refresh_from_db()
        self.assertEqual(doc_b.status, Document.Status.DRAFT)
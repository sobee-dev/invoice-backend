# billing/tests/test_limits.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from documents.models import Document
from products.models import Product
from products.services import LOCKED_PRODUCT_MESSAGE

from .helpers import invoice_payload, make_business, make_owner, make_products, set_plan


class LimitTestBase(APITestCase):
    plan = 'free'

    def setUp(self):
        self.owner = make_owner()
        self.business = make_business(self.owner)
        set_plan(self.business, self.plan)
        self.client.force_authenticate(self.owner)

    def post_invoice(self, **kwargs):
        return self.client.post('/api/documents/', invoice_payload(**kwargs), format='json')

    def post_product(self, name):
        return self.client.post('/api/products/', {'name': name, 'unit_price': '10'}, format='json')


class InvoiceLimitTests(LimitTestBase):

    def test_fifth_invoice_allowed_and_sixth_refused(self):
        for _ in range(5):
            self.assertEqual(self.post_invoice().status_code, 201)
        response = self.post_invoice()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')
        self.assertEqual(response.data['resource'], 'invoices')
        self.assertEqual(response.data['limit'], 5)
        self.assertEqual(response.data['current'], 5)
        self.assertEqual(Document.all_objects.filter(business=self.business).count(), 5)

    def test_proforma_invoices_count_toward_the_limit(self):
        for _ in range(3):
            self.assertEqual(self.post_invoice().status_code, 201)
        for _ in range(2):
            self.assertEqual(self.post_invoice(document_type='proforma_invoice').status_code, 201)
        self.assertEqual(self.post_invoice().status_code, 403)

    def test_supplier_orders_do_not_count(self):
        for _ in range(5):
            self.post_invoice()
        self.assertEqual(self.post_invoice(document_type='purchase_invoice').status_code, 201)

    def test_soft_deleted_invoices_still_count(self):
        for _ in range(5):
            self.post_invoice()
        Document.objects.filter(business=self.business).first().soft_delete()
        self.assertEqual(self.post_invoice().status_code, 403)

    def test_only_the_current_month_counts(self):
        for _ in range(5):
            self.post_invoice()
        Document.all_objects.filter(business=self.business).update(
            created_at=timezone.now() - timedelta(days=40),
        )
        self.assertEqual(self.post_invoice().status_code, 201)


class BusinessPlanInvoiceTests(LimitTestBase):
    plan = 'business'

    def test_invoices_are_unlimited(self):
        for _ in range(6):
            self.assertEqual(self.post_invoice().status_code, 201)


class ProductLimitTests(LimitTestBase):

    def setUp(self):
        super().setUp()
        make_products(self.business, 9)

    def test_tenth_product_allowed_and_eleventh_refused(self):
        self.assertEqual(self.post_product('Tenth').status_code, 201)
        response = self.post_product('Eleventh')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')
        self.assertEqual(response.data['resource'], 'products')
        self.assertEqual(response.data['limit'], 10)

    def test_inactive_products_do_not_count(self):
        self.post_product('Tenth')
        Product.objects.filter(name='Item 0').update(is_active=False)
        self.assertEqual(self.post_product('Replacement').status_code, 201)

    def test_reactivating_over_the_cap_is_refused(self):
        self.post_product('Tenth')
        sleeper = Product.objects.get(name='Item 0')
        Product.objects.filter(pk=sleeper.pk).update(is_active=False)
        self.post_product('Replacement')
        response = self.client.patch(f'/api/products/{sleeper.pk}/', {'is_active': True}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')


class InlineProductTests(LimitTestBase):

    def test_new_product_name_on_an_invoice_at_the_cap_refuses_the_invoice(self):
        make_products(self.business, 10)
        response = self.post_invoice(product_name='Brand New')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['resource'], 'products')
        self.assertEqual(Document.all_objects.filter(business=self.business).count(), 0)
        self.assertEqual(Product.objects.filter(business=self.business).count(), 10)

    def test_existing_product_name_on_an_invoice_is_fine_at_the_cap(self):
        make_products(self.business, 10)
        self.assertEqual(self.post_invoice(product_name='Item 0').status_code, 201)

    def test_new_product_name_under_the_cap_creates_the_product(self):
        make_products(self.business, 9)
        self.assertEqual(self.post_invoice(product_name='Brand New').status_code, 201)
        self.assertEqual(Product.objects.filter(business=self.business, is_active=True).count(), 10)


class FirstTenProductsWindowTests(LimitTestBase):
    plan = 'business'

    def setUp(self):
        super().setUp()
        make_products(self.business, 12)  # built up on a bigger plan
        set_plan(self.business, 'free')   # then downgraded

    def test_only_the_first_ten_are_listed(self):
        response = self.client.get('/api/products/')
        self.assertEqual(response.status_code, 200)
        names = {row['name'] for row in response.data['results']}
        self.assertEqual(len(names), 10)
        self.assertNotIn('Item 10', names)
        self.assertNotIn('Item 11', names)

    def test_a_hidden_product_cannot_be_opened(self):
        hidden = Product.objects.get(name='Item 10')
        self.assertEqual(self.client.get(f'/api/products/{hidden.pk}/').status_code, 404)

    def test_invoice_line_picking_a_hidden_product_by_id_shows_the_message_under_the_line(self):
        hidden = Product.objects.get(name='Item 10')
        items = [
            {'product': str(Product.objects.get(name='Item 0').pk), 'description': 'ok', 'quantity': '1', 'unit_price': '10'},
            {'product': str(hidden.pk), 'description': 'locked', 'quantity': '1', 'unit_price': '10'},
        ]
        response = self.post_invoice(items=items)
        self.assertEqual(response.status_code, 400)
        message = LOCKED_PRODUCT_MESSAGE.format(cap=10)
        self.assertEqual(response.data['items'][0], {})
        self.assertEqual(str(response.data['items'][1]['product'][0]), message)
        self.assertEqual(Document.all_objects.filter(business=self.business).count(), 0)

    def test_invoice_line_typing_a_hidden_product_name_shows_the_message_under_the_line(self):
        response = self.post_invoice(product_name='Item 11')
        self.assertEqual(response.status_code, 400)
        message = LOCKED_PRODUCT_MESSAGE.format(cap=10)
        self.assertEqual(str(response.data['items'][0]['product_name'][0]), message)

    def test_a_new_product_cannot_be_created_while_over_the_cap(self):
        response = self.post_product('Another')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')

    def test_upgrading_shows_everything_again(self):
        set_plan(self.business, 'business')
        response = self.client.get('/api/products/')
        self.assertEqual(len(response.data['results']), 12)


class ProductDeleteTests(LimitTestBase):
    plan = 'business'

    def test_owner_can_delete_and_the_image_is_removed_after_commit(self):
        from unittest import mock
        product = make_products(self.business, 1)[0]
        Product.objects.filter(pk=product.pk).update(image_url='https://res.cloudinary.com/x/image/upload/v1/product-images/a.png')
        with mock.patch('business.services.delete_cloudinary_asset') as destroy:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.delete(f'/api/products/{product.pk}/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
        destroy.assert_called_once()

    def test_product_on_a_draft_invoice_cannot_be_deleted(self):
        self.post_invoice(product_name='Cable')
        product = Product.objects.get(name='Cable')
        response = self.client.delete(f'/api/products/{product.pk}/')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'product_in_use')
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())


class StatusEndpointTests(LimitTestBase):

    def test_usage_is_accurate_on_free(self):
        make_products(self.business, 4)
        for _ in range(3):
            self.post_invoice(product_name='Item 0')
        response = self.client.get('/api/billing/status/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['plan'], 'free')
        self.assertEqual(response.data['limits']['invoices'], 5)
        self.assertEqual(response.data['usage']['invoices'], 3)
        self.assertEqual(response.data['usage']['products'], 4)
        self.assertEqual(response.data['usage']['branches'], 1)
        self.assertIsNone(response.data['days_left_in_trial'])
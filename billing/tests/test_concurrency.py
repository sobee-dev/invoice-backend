# billing/tests/test_concurrency.py
import threading
import time
from decimal import Decimal
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from billing.exceptions import PlanLimitReached
from billing.plans import PRODUCTS, check_limit
from products.models import Product

from .helpers import make_business, make_owner, make_products, set_plan


@skipUnless(connection.vendor == 'postgresql', 'Row locks only exist on Postgres, SQLite skips this test.')
class SimultaneousCreateTests(TransactionTestCase):

    def test_two_requests_cannot_both_take_the_last_slot(self):
        business = make_business(make_owner())
        set_plan(business, 'free')
        make_products(business, 9)  # one slot left on Free

        outcomes = []
        barrier = threading.Barrier(2)

        def attempt(name):
            try:
                barrier.wait(timeout=5)
                with transaction.atomic():
                    check_limit(business, PRODUCTS)
                    time.sleep(0.5)  # hold the lock while the other request arrives
                    Product.objects.create(business=business, name=name, unit_price=Decimal('10'))
                outcomes.append('created')
            except PlanLimitReached:
                outcomes.append('refused')
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(f'Racer {i}',)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcomes), ['created', 'refused'])
        self.assertEqual(Product.objects.filter(business=business, is_active=True).count(), 10)
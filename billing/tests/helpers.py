# billing/tests/helpers.py
from datetime import date
from decimal import Decimal

from accounts.models import User
from billing.models import Subscription
from billing.services import ensure_subscription
from business.models import Business
from products.models import Product
from staff.models import StaffMember

PASSWORD = '482916'


def make_owner(email='owner@test.com'):
    return User.objects.create_user(email=email, password=PASSWORD, role='owner', first_name='Ada')


def make_business(owner, name='Test Business'):
    business = Business.objects.create(
        owner=owner, name=name, address_one='1 Main Street', phone='08012345678',
        email='biz@test.com', signature_type='none',
    )
    ensure_subscription(business)  # a trial, exactly as business creation does
    return business


def set_plan(business, tier, status='active', **fields):
    """Puts the subscription row straight into a state, no Paystack involved."""
    Subscription.objects.filter(business=business).update(plan_tier=tier, status=status, **fields)


def make_staff(business, email, status='active'):
    user = User.objects.create_user(email=email, password=PASSWORD, role='staff', first_name='Staff')
    StaffMember.objects.create(user=user, business=business, status=status)
    return user


def make_products(business, count, start=0):
    return [
        Product.objects.create(business=business, name=f'Item {i}', unit_price=Decimal('100.00'))
        for i in range(start, start + count)
    ]


def invoice_payload(product_name='Cable', document_type='sales_invoice', items=None):
    return {
        'document_type': document_type,
        'document_date': date.today().isoformat(),
        'items': items if items is not None else [
            {'product_name': product_name, 'description': product_name, 'quantity': '1', 'unit_price': '500'},
        ],
    }
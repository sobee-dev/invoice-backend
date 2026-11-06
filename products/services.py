# products/services.py
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from billing.plans import PRODUCTS, check_limit, get_effective_plan
from notifications.services import notify

from .models import Product

LOW_STOCK_COOLDOWN_HOURS = 24
OUT_OF_STOCK_COOLDOWN_HOURS = 24

# Shown under an invoice line that uses a product the plan no longer allows.
LOCKED_PRODUCT_MESSAGE = "only first {cap} products in your inventory are allowed for your plan"


def find_product_by_name(business, name):
    return Product.objects.filter(business=business, name__iexact=name.strip()).first()


def get_or_create_product(business, name, unit_price=0):
    """
    Returns the business's product with this name, creating it if needed. This
    is the single funnel for inline product creation on invoices, so the plan's
    product cap is enforced here. The check and the create share one transaction.
    """
    name = name.strip()
    product = find_product_by_name(business, name)
    if product:
        return product
    with transaction.atomic():
        check_limit(business, PRODUCTS)
        return Product.objects.create(
            business=business,
            name=name,
            unit_price=unit_price,
            sku=None,
        )


# Plan product window
# On a plan with a product cap only the first `cap` active products (oldest
# first) are usable. The rest stay in the database but are hidden from lists and
# refused on invoices until the business upgrades or removes products.

def visible_products(business):
    """Queryset the product list and detail views use."""
    qs = Product.objects.filter(business=business)
    cap = get_effective_plan(business).limits.get(PRODUCTS)
    if cap is None:
        return qs
    window = (
        Product.objects.filter(business=business, is_active=True)
        .order_by('created_at', 'id').values('pk')[:cap]
    )
    return qs.filter(Q(is_active=False) | Q(pk__in=window))


def locked_product_info(business):
    """(ids of active products outside the window, message to show). Empty set when uncapped."""
    cap = get_effective_plan(business).limits.get(PRODUCTS)
    if cap is None:
        return set(), None
    locked = set(
        Product.objects.filter(business=business, is_active=True)
        .order_by('created_at', 'id').values_list('pk', flat=True)[cap:]
    )
    return locked, LOCKED_PRODUCT_MESSAGE.format(cap=cap)


def assert_items_within_plan(business, items_data):
    """
    Called while validating an invoice. A line that uses a locked product, picked
    by id or matched by name, gets an error under that line and the whole
    invoice is refused. Raised in the shape DRF uses for nested item errors:
    {"items": [{}, {"product_name": ["..."]}]}.
    """
    locked, message = locked_product_info(business)
    if not locked:
        return

    errors, found = [], False
    for item in items_data:
        product = item.get('product')
        by_name = product is None and bool((item.get('product_name') or '').strip())
        if by_name:
            product = find_product_by_name(business, item['product_name'])
        if product is not None and product.pk in locked:
            found = True
            errors.append({('product_name' if by_name else 'product'): [message]})
        else:
            errors.append({})
    if found:
        raise serializers.ValidationError({'items': errors})


def _fmt_qty(value: Decimal) -> str:
    """Render a Decimal quantity without trailing zeros: 5 instead of 5.000, 4.5 stays 4.5."""
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return str(value.normalize())


def check_and_notify_stock_level(product: Product) -> None:
    """
    Call this immediately after any save that changes a product's
    quantity_on_hand or quantity_reserved, currently that's
    ProductViewSet.adjust_stock and ProductViewSet.bulk_deduct.

    Fires Out of Stock or Low Stock through the notification dispatch
    layer. The two are mutually exclusive: a product at zero
    available stock only ever triggers Out of Stock, never both.

    Uses available_to_sell (quantity_on_hand - quantity_reserved)
    rather than raw quantity_on_hand, since reserved units aren't
    actually sellable. This is stricter than the Product.is_low_stock
    property, which only checks quantity_on_hand against reorder_level
    and ignores quantity_reserved, so the two can disagree for a product
    with active reservations.

    Throttled per-product via a time cooldown rather than a stored
    state flag, so a product that stays below threshold across many
    sales in a day triggers at most one notification per cooldown
    window, not one per sale.
    """
    available = product.quantity_on_hand - product.quantity_reserved

    if available <= 0:
        notify(
            business=product.business,
            notification_type='out_of_stock',
            title=f"Out of stock: {product.name}",
            body=(
                f"Out of stock: {product.name} has reached 0 units. "
                f"Restock before your next sale."
            ),
            data={'product_id': str(product.id)},
            dedup_key=str(product.id),
            cooldown_hours=OUT_OF_STOCK_COOLDOWN_HOURS,
        )
        return

    if product.reorder_level is not None and available <= product.reorder_level:
        notify(
            business=product.business,
            notification_type='low_stock',
            title=f"Low stock: {product.name}",
            body=(
                f"Low stock: {product.name} is down to {_fmt_qty(available)} units. "
                f"Consider restocking."
            ),
            data={'product_id': str(product.id)},
            dedup_key=str(product.id),
            cooldown_hours=LOW_STOCK_COOLDOWN_HOURS,
        )
from decimal import Decimal

from .models import Product
from notifications.services import notify

LOW_STOCK_COOLDOWN_HOURS = 24
OUT_OF_STOCK_COOLDOWN_HOURS = 24


def get_or_create_product(business, name, unit_price=0):
    name = name.strip()
    product = Product.objects.filter(business=business, name__iexact=name).first()
    if product:
        return product
    return Product.objects.create(
        business=business, 
        name=name, 
        unit_price=unit_price,
        sku=None,
    )


def _fmt_qty(value: Decimal) -> str:
    """Render a Decimal quantity without trailing zeros — 5 instead of 5.000, 4.5 stays 4.5."""
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return str(value.normalize())


def check_and_notify_stock_level(product: Product) -> None:
    """
    Call this immediately after any save that changes a product's
    quantity_on_hand or quantity_reserved — currently that's
    ProductViewSet.adjust_stock and ProductViewSet.bulk_deduct.

    Fires Out of Stock or Low Stock through the notification dispatch
    layer. The two are mutually exclusive — a product at zero
    available stock only ever triggers Out of Stock, never both.

    Uses available_to_sell (quantity_on_hand - quantity_reserved)
    rather than raw quantity_on_hand, since reserved units aren't
    actually sellable. This is stricter than the Product.is_low_stock
    property, which only checks quantity_on_hand against reorder_level
    and ignores quantity_reserved — the two can disagree for a product
    with active reservations. Flagging rather than silently changing
    the existing property's behavior.

    Throttled per-product via a time cooldown rather than a stored
    state flag — a product that stays below threshold across many
    sales in a day triggers at most one notification per cooldown
    window, not one per sale. A "was already low/out" boolean on
    Product would be a more precise alternative if the time-based
    approach turns out to be too coarse in practice.
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
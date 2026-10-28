from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from .models import GroceryItem, RecipeIngredient, StockMovement


def _needs_for_order(order):
    """{ingredient_id: total quantity needed} for the whole order."""
    needs = defaultdict(Decimal)
    for oi in order.items.select_related("food_item"):
        for r in RecipeIngredient.objects.filter(food_item=oi.food_item):
            needs[r.ingredient_id] += r.quantity * oi.quantity
    return needs


@transaction.atomic
def deduct_stock_for_order(order, user=None):
    """Reduce stock using recipes. Safe to call twice: it runs only once per order.
    Turned off when settings.AUTO_DEDUCT_STOCK is False (orders do not touch the store stock)."""
    if not getattr(settings, "AUTO_DEDUCT_STOCK", True):
        return []
    if StockMovement.objects.filter(order_id=order.pk, movement_type=StockMovement.ORDER).exists():
        return []

    needs = _needs_for_order(order)
    if not needs:
        return []

    low_items = []
    items = GroceryItem.objects.select_for_update().filter(pk__in=needs.keys())
    for item in items:
        used = needs[item.pk]
        actual = min(used, max(item.current_stock, Decimal("0")))  # never go below 0
        item.current_stock = item.current_stock - actual
        item.save(update_fields=["current_stock", "updated_at"])
        note = f"Order #{order.pk}"
        if actual < used:
            note += f" (short by {used - actual} {item.unit})"
        StockMovement.objects.create(
            item=item, movement_type=StockMovement.ORDER, change=-actual,
            stock_after=item.current_stock, note=note[:200],
            order_id=order.pk, created_by=user,
        )
        if item.is_out or item.is_low:
            low_items.append(item)
    return low_items


@transaction.atomic
def restore_stock_for_order(order, user=None):
    """Give stock back when an order is cancelled (only if it was deducted)."""
    used = StockMovement.objects.filter(order_id=order.pk, movement_type=StockMovement.ORDER)
    if not used.exists():
        return
    if StockMovement.objects.filter(order_id=order.pk, movement_type=StockMovement.RETURN).exists():
        return
    for mv in used.select_related("item"):
        give_back = -mv.change
        if give_back <= 0:
            continue
        item = GroceryItem.objects.select_for_update().get(pk=mv.item_id)
        item.current_stock += give_back
        item.save(update_fields=["current_stock", "updated_at"])
        StockMovement.objects.create(
            item=item, movement_type=StockMovement.RETURN, change=give_back,
            stock_after=item.current_stock, note=f"Order #{order.pk} cancelled",
            order_id=order.pk, created_by=user,
        )
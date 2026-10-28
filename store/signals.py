from django.db.models.signals import post_save
from django.dispatch import receiver

from orders.models import Order

from .services import deduct_stock_for_order, restore_stock_for_order


@receiver(post_save, sender=Order)
def order_stock_sync(sender, instance, **kwargs):
    if instance.status == "preparing":
        deduct_stock_for_order(instance)
    elif instance.status == "cancelled":
        restore_stock_for_order(instance)
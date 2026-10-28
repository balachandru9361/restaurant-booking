import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from orders.models import Order, OrderItem

from .consumers import GROUP_NAME, user_group_name

logger = logging.getLogger(__name__)

# Any change to a model in one of these apps pings every live admin/store page.
WATCHED_APPS = {
    "orders",
    "dashboard",
    "store",
    "menu",
    "booking",
    "accounts",
}

# Models that are handled by their own receivers below (or are too noisy to watch).
SKIP_MODELS = {"Order", "OrderItem"}


def _send(payload, user_id=None):
    """Send a payload to the staff group (and the customer's group) after the DB commit."""
    layer = get_channel_layer()
    if layer is None:
        return

    def send():
        try:
            async_to_sync(layer.group_send)(GROUP_NAME, payload)
            if user_id:
                async_to_sync(layer.group_send)(user_group_name(user_id), payload)
        except Exception:
            # Never let a realtime failure break saving data.
            logger.exception("Could not broadcast live update")

    transaction.on_commit(send)


def _broadcast(order_id, status, user_id=None, ready_by=None):
    """Notify staff dashboards and the order's customer after the DB commit."""
    payload = {
        "type": "orders_changed",
        "order_id": order_id,
        "status": status,
        "ready_by": ready_by.isoformat() if ready_by else None,
        "kind": "order",
    }
    _send(payload, user_id)


def _broadcast_data_change(label, action):
    """Notify staff pages that some non-order data changed (stock, menu, tables, bookings...)."""
    payload = {
        "type": "orders_changed",
        "order_id": None,
        "status": None,
        "ready_by": None,
        "kind": "data",
        "model": label,
        "action": action,
    }
    _send(payload)


@receiver(post_save, sender=Order)
def order_saved(sender, instance, **kwargs):
    _broadcast(instance.pk, instance.status, instance.user_id, instance.ready_by)


@receiver(post_delete, sender=Order)
def order_deleted(sender, instance, **kwargs):
    _broadcast(instance.pk, "deleted", instance.user_id)


@receiver(post_save, sender=OrderItem)
def order_item_saved(sender, instance, **kwargs):
    order = instance.order
    _broadcast(order.pk, order.status, order.user_id, order.ready_by)


def _is_watched(sender):
    meta = getattr(sender, "_meta", None)
    if meta is None:
        return False
    return meta.app_label in WATCHED_APPS and meta.object_name not in SKIP_MODELS


@receiver(post_save)
def watched_model_saved(sender, instance, created, **kwargs):
    if kwargs.get("raw") or not _is_watched(sender):
        return
    _broadcast_data_change(sender._meta.label, "created" if created else "updated")


@receiver(post_delete)
def watched_model_deleted(sender, instance, **kwargs):
    if not _is_watched(sender):
        return
    _broadcast_data_change(sender._meta.label, "deleted")
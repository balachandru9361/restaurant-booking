from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

GROUP_NAME = "orders"

# Staff groups allowed to listen for live updates (admin, kitchen, server, store pages).
STAFF_GROUPS = {"Admin", "Chef", "Kitchen", "Server", "Waiter", "Store Manager"}


def user_group_name(user_id):
    return f"user_orders_{user_id}"


@database_sync_to_async
def is_staff_user(user):
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name__in=STAFF_GROUPS).exists()


class OrdersConsumer(AsyncJsonWebsocketConsumer):
    """Pushes small live events to staff dashboards (admin, kitchen, store).

    Events:
      - orders_changed: an order was created or its status changed
      - stock_alert: the kitchen reported a product as out of stock
    """

    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated or not await is_staff_user(user):
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    async def orders_changed(self, event):
        await self.send_json({
            "type": "orders_changed",
            "order_id": event.get("order_id"),
            "status": event.get("status"),
            "kind": event.get("kind", "order"),
            "model": event.get("model"),
            "action": event.get("action"),
        })

    async def stock_alert(self, event):
        await self.send_json({
            "type": "stock_alert",
            "product": event.get("product"),
            "item_id": event.get("item_id"),
            "reported_by": event.get("reported_by"),
        })


class MyOrdersConsumer(AsyncJsonWebsocketConsumer):
    """Each logged-in customer only hears about their own orders."""

    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4403)
            return
        self.group = user_group_name(user.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def orders_changed(self, event):
        await self.send_json({
            "type": "orders_changed",
            "order_id": event.get("order_id"),
            "status": event.get("status"),
            "ready_by": event.get("ready_by"),
        })
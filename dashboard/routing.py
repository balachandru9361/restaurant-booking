from django.urls import path

from .consumers import OrdersConsumer, MyOrdersConsumer

websocket_urlpatterns = [
    path("ws/orders/", OrdersConsumer.as_asgi()),
    path("ws/my-orders/", MyOrdersConsumer.as_asgi()),
]
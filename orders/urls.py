from django.urls import path
from . import views

app_name = 'orders'

urlpatterns = [
    path('cart/', views.view_cart, name='view_cart'),
    path('add/<int:item_id>/', views.add_to_cart, name='add_to_cart'),
    path('add-order/<int:item_id>/', views.add_to_order, name='add_to_order'),
    path('bulk-add/', views.bulk_add_to_cart, name='bulk_add_to_cart'),
    path('remove/<int:item_id>/', views.remove_from_cart, name='remove_from_cart'),
    path('coupon/apply/', views.apply_coupon, name='apply_coupon'),
    path('coupon/remove/', views.remove_coupon, name='remove_coupon'),
    path('confirm/', views.confirm_order, name='confirm_order'),
    path('payment/initiate/<int:order_id>/', views.initiate_payment, name='initiate_payment'),
    path('payment/success/<int:order_id>/', views.payment_success, name='payment_success'),
    path('history/', views.order_history, name='order_history'),
    path('table/<int:table_id>/', views.select_table, name='select_table'),
]
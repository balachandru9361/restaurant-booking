from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.admin_dashboard, name='admin_home'),
    path('menu/', views.manage_menu, name='manage_menu'),
    path('menu/add/', views.add_menu_item, name='add_menu_item'),
    path('menu/edit/<int:pk>/', views.edit_menu_item, name='edit_menu_item'),
    path('menu/delete/<int:pk>/', views.delete_menu_item, name='delete_menu_item'),
    path('tables/', views.manage_tables, name='manage_tables'),
    path('tables/add/', views.add_table, name='add_table'),
    path('tables/edit/<int:pk>/', views.edit_table, name='edit_table'),
    path('tables/delete/<int:pk>/', views.delete_table, name='delete_table'),
    path('bookings/', views.manage_bookings, name='manage_bookings'),
    path('users/', views.manage_users, name='manage_users'),
    path('orders/', views.manage_orders, name='manage_orders'),
    path('reports/', views.sales_reports, name='sales_reports'),
    path('kitchen/', views.kitchen_dashboard, name='kitchen_dashboard'),
    path('kitchen/<int:pk>/status/<str:new_status>/', views.update_order_status, name='update_order_status'),
    path('table-qr-codes/', views.table_qr_codes, name='table_qr_codes'),
]

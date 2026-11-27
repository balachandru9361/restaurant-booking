from django.views.generic import RedirectView
from django.urls import path
from . import views
from . import seating_views

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
    path('tables/<int:pk>/open/', views.open_table, name='open_table'),
    path('bookings/', views.manage_bookings, name='manage_bookings'),
    path('bookings/<int:pk>/approve/', views.approve_booking, name='approve_booking'),
    path('bookings/<int:pk>/reject/', views.reject_booking, name='reject_booking'),
    path('users/', views.manage_users, name='manage_users'),
    path('users/add-staff/', views.add_staff, name='add_staff'),
    path('users/bulk-action/', views.bulk_user_action, name='bulk_user_action'),
    path('orders/', views.manage_orders, name='manage_orders'),
    path('orders/<int:pk>/update-status/', views.api_order_status, name='api_order_status'),
    path('orders/<int:pk>/assign-table/', views.assign_table, name='assign_table'),
    path('orders/<int:pk>/mark-cash-received/', views.mark_cash_received, name='mark_cash_received'),
    path('orders/<int:pk>/mark-refunded/', views.mark_refunded, name='mark_refunded'),
    path('orders/<int:pk>/approve/', views.approve_order, name='approve_order'),
    path('orders/<int:pk>/reject/', views.reject_order, name='reject_order'),
    path('orders/<int:pk>/stock-check/', views.order_stock_check, name='order_stock_check'),
    path('grocery/', RedirectView.as_view(pattern_name='store:item_list'), name='manage_grocery'),
    path('grocery/<int:pk>/update/', views.update_grocery, name='update_grocery'),
    path('grocery/<int:pk>/delete/', views.delete_grocery, name='delete_grocery'),
    path('reports/', views.sales_reports, name='sales_reports'),
    path('kitchen/', views.kitchen_dashboard, name='kitchen_dashboard'),
    path('kitchen/<int:pk>/status/<str:new_status>/', views.update_order_status, name='update_order_status'),
    path('kitchen/grocery/<int:pk>/update/', views.kitchen_update_grocery, name='kitchen_update_grocery'),
    path('server/', views.server_dashboard, name='server_dashboard'),
    path('server/poll/', views.server_poll, name='server_poll'),
    path('server/take-order/', views.server_take_order, name='server_take_order'),
    path('server/free-table/<int:pk>/', views.server_free_table, name='server_free_table'),
    path('server/mark-cash-received/<int:pk>/', views.server_mark_cash_received, name='server_mark_cash_received'),
    path('notifications/poll/', views.notifications_poll, name='notifications_poll'),
    path('notifications/read/', views.notifications_mark_read, name='notifications_mark_read'),
    path('poll/admin/', views.admin_poll, name='admin_poll'),   # NEW: live update for all admin pages
    path('table-qr-codes/', views.table_qr_codes, name='table_qr_codes'),
    path('kitchen/stock/', views.kitchen_stock, name='kitchen_stock'),
    path('kitchen/stock/<int:pk>/request/', views.kitchen_request_stock, name='kitchen_request_stock'),
    path('kitchen/notifications/poll/', views.kitchen_notifications_poll, name='kitchen_notifications_poll'),
    path('kitchen/stock-update/', views.kitchen_stock_update, name='kitchen_stock_update'),
    path('menu/<int:pk>/toggle/', views.toggle_menu_availability, name='toggle_menu_availability'),
    path('server/send-to-kitchen/<int:pk>/', views.server_send_to_kitchen, name='server_send_to_kitchen'),
    path('server/reject/<int:pk>/', views.server_reject_order, name='server_reject_order'),
    path('server/cancellations/<int:pk>/decision/', views.server_decide_cancellation, name='server_decide_cancellation'),
    path('server/cancel-unpaid/<int:pk>/', views.server_cancel_unpaid_order, name='server_cancel_unpaid_order'),
    path('server/bill/<int:pk>/', views.server_table_bill, name='server_table_bill'),
    path('server/bill/<int:pk>/cash-received/', views.server_table_cash_received, name='server_table_cash_received'),

    # NEW: Walk-in seating (seat based). All of these live in dashboard/seating_views.py
    path('server/walkin-check/', seating_views.server_walkin_check, name='server_walkin_check'),
    path('server/walkin-confirm/', seating_views.server_walkin_confirm, name='server_walkin_confirm'),
    path('server/seating/take-order/', seating_views.server_take_session_order, name='server_take_session_order'),
    path('server/seating/tables/', seating_views.server_tables_overview, name='server_tables_overview'),
    path('server/seating/panel/', seating_views.server_seating_panel, name='server_seating_panel'),
    path('server/seating/individual-bill/', seating_views.server_individual_bill_create, name='server_individual_bill_create'),   # NEW: one customer = one bill
    path('server/billing/<int:table_id>/', seating_views.server_table_billing, name='server_table_billing'),   # NEW: Table billing dashboard
    path('server/seating/<int:pk>/cancel/', seating_views.server_seating_cancel, name='server_seating_cancel'),   # NEW: customer changed their mind
    path('server/seating/<int:pk>/departed/', seating_views.server_seating_departed, name='server_seating_departed'),
    path('server/seating/<int:pk>/cleaned/', seating_views.server_seating_cleaned, name='server_seating_cleaned'),
]
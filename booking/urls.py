from django.urls import path
from . import views

urlpatterns = [
    path('book/', views.book_table, name='book_table'),
    path('success/', views.booking_success, name='booking_success'),
    path('history/', views.booking_history, name='booking_history'),
    path('cancel/<int:booking_id>/', views.cancel_booking, name='cancel_booking'),
    path('check-availability/', views.check_availability, name='check_availability'),
    path('table-status/', views.table_status_for_slot, name='table_status'),
    path('bookings-for-date/', views.bookings_for_date, name='bookings_for_date'),
]
from django.contrib import admin
from .models import TableBooking, Table


@admin.register(TableBooking)
class TableBookingAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'guests', 'booking_date', 'booking_time', 'table', 'released', 'user')
    list_filter = ('booking_date', 'released')
    search_fields = ('name', 'phone')


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('number', 'capacity')
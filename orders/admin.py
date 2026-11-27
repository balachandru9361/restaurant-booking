from django.contrib import admin
from .models import Order, OrderItem, Coupon


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user', 'status', 'cancellation_status', 'payment_status',
        'refund_status', 'coupon', 'discount_amount', 'total_amount', 'created_at',
    )
    list_filter = ('status', 'cancellation_status', 'payment_status', 'refund_status', 'created_at')
    search_fields = ('id', 'guest_name', 'guest_phone', 'cancellation_reason')
    inlines = [OrderItemInline]


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ('code', 'discount_percent', 'is_active', 'valid_from', 'valid_until', 'times_used', 'usage_limit')
    list_filter = ('is_active',)
    search_fields = ('code',)
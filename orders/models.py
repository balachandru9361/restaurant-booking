from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from menu.models import FoodItem
from booking.models import Table


class Coupon(models.Model):
    code = models.CharField(max_length=20, unique=True)
    discount_percent = models.PositiveIntegerField(help_text="e.g. 10 for 10% off")
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField()
    usage_limit = models.PositiveIntegerField(null=True, blank=True, help_text="Leave blank for unlimited")
    times_used = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.code} ({self.discount_percent}% off)"

    def is_valid(self):
        now = timezone.now()
        if not self.is_active or now < self.valid_from or now > self.valid_until:
            return False
        if self.usage_limit and self.times_used >= self.usage_limit:
            return False
        return True


class Order(models.Model):
    # Flow: pending -> confirmed -> preparing -> ready
    #   dine-in / takeaway : ready -> delivered ("Served" in the staff dashboards)
    #   home delivery      : ready -> out_for_delivery -> delivered
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('preparing', 'Preparing'),
        ('ready', 'Ready'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),  # shown as "Served" in the staff dashboards
        ('cancelled', 'Cancelled'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('online', 'Online Payment'),
        ('cash', 'Cash at Restaurant'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('cash_pending', 'Cash Pending'),
    ]

    ORDER_TYPE_CHOICES = [
        ('dine_in', 'Dine-in'),
        ('takeaway', 'Take away'),
        ('delivery', 'Home delivery'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders')
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    # How the customer wants the order, when they will arrive, and cooking notes
    order_type = models.CharField(max_length=10, choices=ORDER_TYPE_CHOICES, default='dine_in')
    arrival_time = models.DateTimeField(
        null=True, blank=True,
        help_text="When the customer will arrive. Blank means as soon as possible."
    )
    note = models.CharField(max_length=255, blank=True)

    # Home delivery details (only used when order_type is 'delivery')
    delivery_address = models.TextField(blank=True)
    delivery_phone = models.CharField(max_length=15, blank=True)
    delivery_person = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deliveries',
        help_text="Delivery staff who picked up this order.",
    )

    # Payment tracking
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default='online')
    payment_status = models.CharField(max_length=15, choices=PAYMENT_STATUS_CHOICES, default='pending')

    # Admin can open the table early (before the 2-hour hold ends)
    table_released = models.BooleanField(default=False)

    # True once the admin has paid the money back for a cancelled online order
    refunded = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Order #{self.id} - {self.user.username}"

    @property
    def is_takeaway(self):
        return self.order_type == 'takeaway'

    @property
    def is_delivery(self):
        return self.order_type == 'delivery'


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    food_item = models.ForeignKey(FoodItem, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=8, decimal_places=2)

    def subtotal(self):
        return self.quantity * self.price

    def __str__(self):
        return f"{self.food_item.name} x {self.quantity}"
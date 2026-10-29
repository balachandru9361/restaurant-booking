from datetime import timedelta

from django.core.validators import RegexValidator
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from menu.models import FoodItem
from booking.models import Table

# An online order that is still unpaid after this many minutes is expired.
PAYMENT_HOLD_MINUTES = 15

phone_validator = RegexValidator(
    regex=r'^[0-9]{10}$',
    message='Enter a valid 10-digit phone number.',
)


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


class OrderQuerySet(models.QuerySet):
    """Helpers so every staff screen uses the same payment-first rule."""

    def visible_to_staff(self):
        # Online orders reach the admin / server / kitchen only after payment.
        # Orders taken by the server (cash at the counter) are always visible.
        return self.exclude(
            payment_method='online',
            payment_status__in=['pending', 'expired'],
        )

    def awaiting_payment(self):
        return self.filter(payment_method='online', payment_status='pending')

    def stale_unpaid(self, minutes=PAYMENT_HOLD_MINUTES):
        cutoff = timezone.now() - timedelta(minutes=minutes)
        return self.awaiting_payment().filter(created_at__lt=cutoff)


class Order(models.Model):
    # Flow: pending -> confirmed -> preparing -> ready
    #   dine-in / takeaway : ready -> delivered ("Served" in the staff dashboards)
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('preparing', 'Preparing'),
        ('ready', 'Ready'),
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
        ('expired', 'Expired'),  # online order that was never paid
    ]

    ORDER_TYPE_CHOICES = [
        ('dine_in', 'Dine-in'),
        ('takeaway', 'Take away'),
    ]

    # Who placed the order: the customer (QR scan / website) or a server.
    SOURCE_CHOICES = [
        ('qr', 'QR / Customer'),
        ('server', 'Server'),
    ]

    # null=True: a guest order (no login) has no user. SET_NULL: if a customer
    # account is deleted, the order stays for the restaurant's sales records.
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders'
    )

    # Details the customer types on the cart page (no customer login)
    guest_name = models.CharField(max_length=100, blank=True)
    guest_phone = models.CharField(max_length=15, blank=True, validators=[phone_validator])

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

    # Payment tracking
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default='online')
    payment_status = models.CharField(max_length=15, choices=PAYMENT_STATUS_CHOICES, default='pending')

    # Razorpay payment id (pay_xxxxx). Saved when the online payment is verified.
    # Needed to refund the customer automatically when the order is rejected.
    razorpay_payment_id = models.CharField(max_length=100, blank=True, default='')

    # Admin can open the table early (before the 2-hour hold ends)
    table_released = models.BooleanField(default=False)

    # True once the money has been paid back for a cancelled online order
    # (automatically through Razorpay, or by the admin pressing "Mark Refunded")
    refunded = models.BooleanField(default=False)

    # Time the kitchen promised the order will be ready.
    # Set by the chef when preparing starts; blank until then.
    ready_by = models.DateTimeField(
        null=True, blank=True,
        help_text="Estimated ready time chosen by the chef when preparing starts."
    )

    # The chef who clicked "Start Preparing" for this order.
    # SET_NULL: if the chef's login is deleted later, the order stays safe.
    prepared_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prepared_orders',
        help_text="Chef who took this order (set when preparing starts)."
    )

    # Where the order came from: customer QR scan / website, or a server.
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='qr')

    # The server who took this order from the "Take Order" screen.
    # Blank for QR / website orders. SET_NULL keeps the order if the server's login is deleted.
    taken_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='taken_orders',
        help_text="Server who took this order (blank for QR / customer orders)."
    )

    objects = OrderQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Order #{self.id} - {self.customer_name}"

    @property
    def customer_name(self):
        """Name to show in dashboards, emails and templates."""
        if self.guest_name:
            return self.guest_name
        if self.user_id:
            return self.user.get_full_name() or self.user.username
        return 'Guest'

    @property
    def customer_phone(self):
        return self.guest_phone

    @property
    def is_guest(self):
        return self.user_id is None

    @property
    def is_takeaway(self):
        return self.order_type == 'takeaway'

    @property
    def is_server_order(self):
        return self.source == 'server'

    @property
    def taken_by_name(self):
        """Server name to show on dashboard cards (blank for QR orders)."""
        if not self.taken_by_id:
            return ''
        return self.taken_by.get_full_name() or self.taken_by.username

    @property
    def awaiting_payment(self):
        return self.payment_method == 'online' and self.payment_status == 'pending'

    def expire_if_unpaid(self):
        """Cancel an online order that was never paid. Returns True if it was expired."""
        if not self.awaiting_payment:
            return False
        self.payment_status = 'expired'
        self.status = 'cancelled'
        self.save(update_fields=['payment_status', 'status'])
        return True


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    # PROTECT: a dish that was ordered before cannot be deleted, so old orders
    # and sales reports stay correct. Hide the dish with is_available=False instead.
    food_item = models.ForeignKey(FoodItem, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    # Chosen options for this line (for the kitchen). The price above already includes the extras.
    options_text = models.CharField(max_length=500, blank=True)

    def subtotal(self):
        return self.quantity * self.price

    def __str__(self):
        return f"{self.food_item.name} x {self.quantity}"
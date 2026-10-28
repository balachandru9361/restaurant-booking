from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.contrib.auth.models import User


class Table(models.Model):
    number = models.PositiveIntegerField(unique=True)
    capacity = models.PositiveIntegerField()

    def __str__(self):
        return f"Table {self.number} (seats {self.capacity})"


class TableBooking(models.Model):
    OCCASION_CHOICES = [
        ('none', 'No specific occasion'),
        ('birthday', 'Birthday'),
        ('anniversary', 'Anniversary'),
        ('family', 'Family gathering'),
        ('business', 'Business meeting'),
        ('date', 'Date night'),
        ('other', 'Other'),
    ]

    # A booking waits for the admin before it is confirmed.
    # 'cancelled' means the customer cancelled it; the record is kept.
    STATUS_CHOICES = [
        ('pending', 'Waiting for admin approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='table_bookings')

    # Tables are chosen by the admin when the booking is approved.
    # A new customer request has no table (table is empty) until then.
    # `table` is the main table, `extra_tables` are the additional tables
    # for bigger parties.
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    extra_tables = models.ManyToManyField(Table, blank=True, related_name='extra_bookings')

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    # Up to 30 guests.
    guests = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(30)],
    )

    booking_date = models.DateField()
    booking_time = models.TimeField()

    occasion = models.CharField(max_length=20, choices=OCCASION_CHOICES, default='none')
    special_request = models.TextField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # Admin approval
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    reject_reason = models.CharField(max_length=200, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    # The admin clicks "Open Table" to free the booked tables.
    # Tables are never freed automatically by a timer.
    released = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-booking_date', '-booking_time']

    @property
    def all_tables(self):
        """Every table of this booking (main + extra), sorted by table number.
        Customer pass, server dashboard and admin pages must all read tables from here
        so they always show the same thing. Works only after the booking is saved."""
        tables = []
        if self.table_id:
            tables.append(self.table)
        if self.pk:
            tables.extend(t for t in self.extra_tables.all() if t.id != self.table_id)
        return sorted(tables, key=lambda t: t.number)

    @property
    def has_tables(self):
        """True once the admin has chosen at least one table."""
        return len(self.all_tables) > 0

    @property
    def table_numbers(self):
        """Table numbers in order, for display. Example: 'Tables 1, 2, 5'."""
        numbers = [t.number for t in self.all_tables]
        if not numbers:
            return ''
        label = 'Table' if len(numbers) == 1 else 'Tables'
        return f"{label} " + ", ".join(str(n) for n in numbers)

    @property
    def total_seats(self):
        """Total seats of all tables chosen for this booking."""
        return sum(t.capacity for t in self.all_tables)

    @property
    def seats_summary(self):
        """Real seat text for the customer pass (replaces the hardcoded '6 seats per table').
        '6 seats' / '6 seats per table' / '6 + 6 + 7 = 19 seats'."""
        caps = [t.capacity for t in self.all_tables]
        if not caps:
            return ''
        if len(caps) == 1:
            return f"{caps[0]} seats"
        if len(set(caps)) == 1:
            return f"{caps[0]} seats per table"
        return " + ".join(str(c) for c in caps) + f" = {sum(caps)} seats"

    def __str__(self):
        return f"{self.name} - {self.booking_date} {self.booking_time}"
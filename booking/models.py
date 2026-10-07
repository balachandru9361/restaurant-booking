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

    # A booking waits for the admin before it is confirmed
    STATUS_CHOICES = [
        ('pending', 'Waiting for admin approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='table_bookings')
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    # NEW: more than 6 guests need more than one table. `table` stays the main table,
    # these are the additional tables given to the same booking.
    extra_tables = models.ManyToManyField(Table, blank=True, related_name='extra_bookings')

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    # One table seats 6, so up to 30 guests means up to 5 tables.
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

    # Admin can open the table early (before the 2-hour hold ends)
    released = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-booking_date', '-booking_time']

    @property
    def all_tables(self):
        """Main table first, then the extra tables (only works after the booking is saved)."""
        tables = []
        if self.table_id:
            tables.append(self.table)
        if self.pk:
            tables.extend(t for t in self.extra_tables.all() if t.id != self.table_id)
        return tables

    def __str__(self):
        return f"{self.name} - {self.booking_date} {self.booking_time}"
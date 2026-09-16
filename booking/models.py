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

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='table_bookings')
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    guests = models.PositiveIntegerField(default=1)

    booking_date = models.DateField()
    booking_time = models.TimeField()

    occasion = models.CharField(max_length=20, choices=OCCASION_CHOICES, default='none')
    special_request = models.TextField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-booking_date', '-booking_time']

    def __str__(self):
        return f"{self.name} - {self.booking_date} {self.booking_time}"
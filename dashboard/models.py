from django.db import models
from django.contrib.auth.models import User


class Notification(models.Model):
    """Messages shown on the admin dashboard (e.g. when a waiter serves an order)."""

    KIND_CHOICES = [
        ('served', 'Order served'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='served')
    message = models.CharField(max_length=255)
    order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return self.message

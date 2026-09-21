from django.db import models
from django.contrib.auth.models import User


class Profile(models.Model):
    """Extra info for staff logins created by the admin.
    Superusers = Admin, regular users (no profile) = Customer.
    Staff (is_staff=True, not superuser) get a role here: kitchen or server."""

    ROLE_CHOICES = [
        ('kitchen', 'Kitchen Staff'),
        ('server', 'Server / Waiter'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

from decimal import Decimal

from django.conf import settings
from django.db import models


class GroceryCategory(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Grocery categories"

    def __str__(self):
        return self.name


class GroceryItem(models.Model):
    UNITS = [
        ("kg", "Kilogram (kg)"),
        ("g", "Gram (g)"),
        ("l", "Litre (l)"),
        ("ml", "Millilitre (ml)"),
        ("pcs", "Pieces"),
        ("pack", "Packet"),
    ]

    name = models.CharField(max_length=120)
    category = models.ForeignKey(
        GroceryCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="items"
    )
    unit = models.CharField(max_length=10, choices=UNITS, default="kg")
    current_stock = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    reorder_level = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0"),
        help_text="Low-stock alert shows when stock is at or below this level.",
    )
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    supplier = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.unit})"

    @property
    def is_out(self):
        return self.current_stock <= 0

    @property
    def is_low(self):
        return self.current_stock > 0 and self.current_stock <= self.reorder_level

    @property
    def stock_value(self):
        return self.current_stock * self.cost_per_unit


class StockMovement(models.Model):
    IN = "IN"
    OUT = "OUT"
    ADJUST = "ADJUST"
    DAMAGE = "DAMAGE"
    EXPIRY = "EXPIRY"
    TYPES = [
        (IN, "Stock In (purchase / received)"),
        (OUT, "Stock Out (used / wasted)"),
        (ADJUST, "Correction (set exact count)"),
        (DAMAGE, "Damaged"),
        (EXPIRY, "Expired"),
    ]

    item = models.ForeignKey(GroceryItem, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=6, choices=TYPES)
    change = models.DecimalField(max_digits=12, decimal_places=3)  # signed: +added, -removed
    stock_after = models.DecimalField(max_digits=12, decimal_places=3)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.item.name}: {self.change:+}"
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q


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

    @property
    def blocks_dishes(self):
        """True when this item is at or below its alert limit, so dishes using it
        must be shown as Sold out. If no limit is set (0), it only blocks when
        stock is 0 or less."""
        if self.reorder_level > 0:
            return self.current_stock <= self.reorder_level
        return self.current_stock <= 0


class StockMovement(models.Model):
    IN = "IN"
    OUT = "OUT"
    ADJUST = "ADJUST"
    DAMAGE = "DAMAGE"
    EXPIRY = "EXPIRY"
    ORDER = "ORDER"    # auto: used by a customer order (recipe)
    RETURN = "RETURN"  # auto: order cancelled, stock given back
    TYPES = [
        (IN, "Stock In (purchase / received)"),
        (OUT, "Stock Out (used / wasted)"),
        (ADJUST, "Correction (set exact count)"),
        (DAMAGE, "Damaged"),
        (EXPIRY, "Expired"),
        (ORDER, "Used in order (auto)"),
        (RETURN, "Returned from cancelled order (auto)"),
    ]

    item = models.ForeignKey(GroceryItem, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=6, choices=TYPES)
    change = models.DecimalField(max_digits=12, decimal_places=3)  # signed: +added, -removed
    stock_after = models.DecimalField(max_digits=12, decimal_places=3)
    note = models.CharField(max_length=200, blank=True)
    # which customer order caused this movement (auto deduction only)
    order_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.item.name}: {self.change:+}"


class RecipeIngredient(models.Model):
    """One line of a recipe: 1 portion of food_item uses `quantity` of `ingredient`.
    Quantity is in the ingredient's own unit (mutton in kg -> 250 g = 0.25)."""

    food_item = models.ForeignKey(
        "menu.FoodItem", on_delete=models.CASCADE, related_name="recipe_items"
    )
    ingredient = models.ForeignKey(
        GroceryItem, on_delete=models.CASCADE, related_name="used_in"
    )
    quantity = models.DecimalField(
        max_digits=10, decimal_places=3,
        help_text="Per 1 portion, in the ingredient's unit (kg item: 250 g = 0.25).",
    )

    class Meta:
        unique_together = ("food_item", "ingredient")
        ordering = ["food_item", "ingredient__name"]

    def __str__(self):
        return f"{self.food_item} -> {self.quantity} {self.ingredient.unit} {self.ingredient.name}"


def sold_out_food_ids():
    """IDs of all dishes that have at least one ingredient at or below its alert limit.
    One query, so the menu page can call it once for all dishes."""
    return set(
        RecipeIngredient.objects.filter(
            Q(ingredient__reorder_level__gt=0,
              ingredient__current_stock__lte=F("ingredient__reorder_level"))
            | Q(ingredient__reorder_level__lte=0,
                ingredient__current_stock__lte=0)
        ).values_list("food_item_id", flat=True)
    )


def sold_out_ingredients(food_item_id):
    """Names of the ingredients that make this dish Sold out (empty list = available)."""
    rows = RecipeIngredient.objects.filter(food_item_id=food_item_id).select_related("ingredient")
    return [r.ingredient.name for r in rows if r.ingredient.blocks_dishes]
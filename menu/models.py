# menu/models.py
from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True, blank=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class FoodItemQuerySet(models.QuerySet):
    def orderable(self):
        """Dishes that are switched on by the admin.
        Use .in_stock() as well to hide dishes whose ingredients are low."""
        return self.filter(is_available=True)

    def in_stock(self):
        """Hide dishes that have any recipe ingredient at or below its alert limit.
        They come back by themselves once the stock goes above the limit."""
        from store.models import sold_out_food_ids  # local import avoids circular import

        return self.exclude(id__in=sold_out_food_ids())

    def with_stock(self):
        """Load recipe ingredients in 2 extra queries so the menu page does not
        run one query per dish when checking sold-out status."""
        return self.prefetch_related("recipe_items__ingredient")


class FoodItem(models.Model):
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="items"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    image = models.ImageField(upload_to="food_images/", blank=True, null=True)
    is_available = models.BooleanField(default=True)
    # True when the system blocked the dish because of low or out of stock
    auto_disabled = models.BooleanField(default=False)
    is_veg = models.BooleanField(default=True)  # True = Veg, False = Non-Veg
    created_at = models.DateTimeField(auto_now_add=True)

    objects = FoodItemQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    # ---- Stock based availability (calculated live, nothing is saved) ----

    @property
    def stock_blockers(self):
        """Names of recipe ingredients at or below their alert limit.
        Works with prefetch (see FoodItemQuerySet.with_stock)."""
        return [
            r.ingredient.name
            for r in self.recipe_items.all()
            if r.ingredient.blocks_dishes
        ]

    @property
    def is_sold_out(self):
        """True when any ingredient of this dish has reached its alert limit.
        A dish with no recipe is never sold out."""
        return len(self.stock_blockers) > 0

    @property
    def can_order(self):
        """True when the customer can add this dish to the cart."""
        return self.is_available and not self.is_sold_out


class FoodItemImage(models.Model):
    food_item = models.ForeignKey(
        FoodItem, on_delete=models.CASCADE, related_name="gallery_images"
    )
    image = models.ImageField(upload_to="food_images/gallery/")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.food_item.name} - image {self.order}"


class OptionGroup(models.Model):
    """A group of choices on a dish, e.g. 'Choose Your Side' or 'Add-ons'."""

    SINGLE = "single"
    MULTI = "multi"
    TYPE_CHOICES = [
        (SINGLE, "Pick one (radio)"),
        (MULTI, "Pick many (checkbox)"),
    ]

    food_item = models.ForeignKey(
        FoodItem, on_delete=models.CASCADE, related_name="option_groups"
    )
    title = models.CharField(max_length=100)
    group_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=SINGLE)
    is_required = models.BooleanField(default=False)
    max_select = models.PositiveIntegerField(
        default=0, help_text="Only for 'Pick many'. 0 means no limit."
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.food_item.name} - {self.title}"


class Option(models.Model):
    """One choice inside an OptionGroup, e.g. 'Regular Fry' or 'No Onion'."""

    group = models.ForeignKey(
        OptionGroup, on_delete=models.CASCADE, related_name="options"
    )
    name = models.CharField(max_length=100)
    extra_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    image = models.ImageField(upload_to="option_images/", blank=True, null=True)
    is_default = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name
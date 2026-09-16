from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True, blank=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ['name']

    def __str__(self):
        return self.name


class FoodItem(models.Model):
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name='items'
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    image = models.ImageField(upload_to='food_images/', blank=True, null=True)
    is_available = models.BooleanField(default=True)
    is_veg = models.BooleanField(default=True)  # True = Veg, False = Non-Veg
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class FoodItemImage(models.Model):
    food_item = models.ForeignKey(
        FoodItem, on_delete=models.CASCADE, related_name='gallery_images'
    )
    image = models.ImageField(upload_to='food_images/gallery/')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.food_item.name} - image {self.order}"
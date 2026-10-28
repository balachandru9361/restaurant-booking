from django.contrib import admin

from .models import GroceryCategory, GroceryItem, RecipeIngredient, StockMovement

admin.site.register(GroceryCategory)


@admin.register(GroceryItem)
class GroceryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit", "current_stock", "reorder_level", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("name", "supplier")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "item", "movement_type", "change", "stock_after", "order_id", "created_by")
    list_filter = ("movement_type",)
    search_fields = ("item__name", "note")


@admin.register(RecipeIngredient)
class RecipeIngredientAdmin(admin.ModelAdmin):
    list_display = ("food_item", "ingredient", "quantity")
    list_filter = ("food_item",)
    search_fields = ("food_item__name", "ingredient__name")
    autocomplete_fields = ("ingredient",)
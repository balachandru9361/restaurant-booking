from django.contrib import admin

from .models import GroceryCategory, GroceryItem, StockMovement

admin.site.register(GroceryCategory)


@admin.register(GroceryItem)
class GroceryItemAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit", "current_stock", "reorder_level", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("name", "supplier")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "item", "movement_type", "change", "stock_after", "created_by")
    list_filter = ("movement_type",)

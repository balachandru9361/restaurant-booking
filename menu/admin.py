from django.contrib import admin
from .models import Category, FoodItem, FoodItemImage


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


class FoodItemImageInline(admin.TabularInline):
    model = FoodItemImage
    extra = 1


@admin.register(FoodItem)
class FoodItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'price', 'is_veg', 'is_available', 'created_at')
    list_filter = ('category', 'is_veg', 'is_available')
    search_fields = ('name', 'description')
    list_editable = ('is_veg', 'is_available')
    inlines = [FoodItemImageInline]    

# ---- Dish options (Choose Your Side, Add-ons, Customise) ----
from django.contrib import admin
from .models import OptionGroup, Option


class OptionInline(admin.TabularInline):
    model = Option
    extra = 2


@admin.register(OptionGroup)
class OptionGroupAdmin(admin.ModelAdmin):
    list_display = ("title", "food_item", "group_type", "is_required", "order")
    list_filter = ("group_type", "is_required")
    search_fields = ("title", "food_item__name")
    inlines = [OptionInline]

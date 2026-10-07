from django.contrib import admin
from .models import Notification, RecipeIngredient


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('message', 'kind', 'is_read', 'created_at')
    list_filter = ('kind', 'is_read')


@admin.register(RecipeIngredient)
class RecipeIngredientAdmin(admin.ModelAdmin):
    list_display = ('food_item', 'grocery', 'qty_per_serving')
    list_filter = ('food_item',)
    search_fields = ('food_item__name', 'grocery__name')
    autocomplete_fields = ()
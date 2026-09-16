from django import forms
from .models import FoodItem

class MenuItemForm(forms.ModelForm):
    class Meta:
        model = FoodItem
        fields = ['category', 'name', 'description', 'price', 'image', 'is_available']
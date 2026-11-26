from django import forms
from .models import FoodItem


class MenuItemForm(forms.ModelForm):
    is_veg = forms.TypedChoiceField(
        label='Food type',
        choices=((True, 'Veg'), (False, 'Non-Veg')),
        coerce=lambda value: value == 'True',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = FoodItem
        fields = [
            'category',
            'name',
            'is_veg',
            'description',
            'price',
            'image',
            'is_available',
        ]

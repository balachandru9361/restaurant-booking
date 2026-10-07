from decimal import Decimal

from django import forms

from .models import GroceryCategory, GroceryItem, StockMovement


class GroceryItemForm(forms.ModelForm):
    opening_stock = forms.DecimalField(
        required=False, min_value=Decimal("0"), max_digits=12, decimal_places=3,
        label="Opening stock", help_text="Current quantity you already have (only when adding).",
    )

    class Meta:
        model = GroceryItem
        fields = ["name", "category", "unit", "reorder_level", "cost_per_unit", "supplier"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:  # editing: stock is changed only through "Update Stock"
            del self.fields["opening_stock"]


class GroceryCategoryForm(forms.ModelForm):
    class Meta:
        model = GroceryCategory
        fields = ["name"]


class StockUpdateForm(forms.Form):
    movement_type = forms.ChoiceField(
        choices=[c for c in StockMovement.TYPES
                 if c[0] in (StockMovement.IN, StockMovement.OUT, StockMovement.ADJUST)],
        initial=StockMovement.IN)
    quantity = forms.DecimalField(min_value=Decimal("0"), max_digits=12, decimal_places=3)
    note = forms.CharField(required=False, max_length=200)

    def clean(self):
        data = super().clean()
        kind, qty = data.get("movement_type"), data.get("quantity")
        if kind in (StockMovement.IN, StockMovement.OUT) and qty is not None and qty <= 0:
            self.add_error("quantity", "Quantity must be greater than 0.")
        return data


class StockMoveForm(forms.Form):
    item = forms.ModelChoiceField(queryset=GroceryItem.objects.filter(is_active=True).order_by("name"))
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=12, decimal_places=3)
    reason = forms.ChoiceField(choices=[], required=False)
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, reasons=None, **kwargs):
        super().__init__(*args, **kwargs)
        if reasons:
            self.fields["reason"].choices = reasons
            self.fields["reason"].required = True
        else:
            del self.fields["reason"]

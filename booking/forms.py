import re
from django import forms
from .models import TableBooking, Table


class TableBookingForm(forms.ModelForm):
    # Hidden field, filled in by the JS table-grid on the booking page.
    # Optional at the form level - if left blank, the view auto-assigns the
    # smallest free table (same fallback behaviour as before).
    table = forms.ModelChoiceField(
        queryset=Table.objects.all(),
        required=False,
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = TableBooking
        fields = ['name', 'phone', 'guests', 'booking_date', 'booking_time', 'occasion', 'special_request', 'table']
        widgets = {
            'booking_date': forms.DateInput(attrs={'type': 'date'}),
            'booking_time': forms.TimeInput(attrs={'type': 'time'}),
            'special_request': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        # Indian mobile numbers: exactly 10 digits, starting with 6, 7, 8, or 9
        if not re.match(r'^[6-9]\d{9}$', phone):
            raise forms.ValidationError(
                'Please enter a valid 10-digit Indian mobile number.'
            )
        return phone


class TableForm(forms.ModelForm):
    class Meta:
        model = Table
        fields = ['number', 'capacity']
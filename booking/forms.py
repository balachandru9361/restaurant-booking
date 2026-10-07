import re
from django import forms
from .models import TableBooking, Table

# One table seats 6 guests. A bigger party gets several tables.
SEATS_PER_TABLE = 6
MAX_GUESTS = 30


def tables_needed(guests):
    """How many tables a party of this size needs (7 guests -> 2 tables)."""
    return -(-int(guests) // SEATS_PER_TABLE)


class TableBookingForm(forms.ModelForm):
    # Hidden field, filled in by the JS table-grid on the booking page.
    # Optional at the form level - if left blank, the view auto-assigns the
    # smallest free table (same fallback behaviour as before).
    table = forms.ModelChoiceField(
        queryset=Table.objects.all(),
        required=False,
        widget=forms.HiddenInput(),
    )

    guests = forms.IntegerField(
        min_value=1,
        max_value=MAX_GUESTS,
        label='Guests',
        help_text=f'One table seats {SEATS_PER_TABLE} guests only. '
                  f'Bigger groups (up to {MAX_GUESTS}) get more than one table.',
        error_messages={
            'min_value': 'At least 1 guest is required.',
            'max_value': f'Maximum {MAX_GUESTS} guests per booking.',
            'invalid': 'Enter a valid number of guests.',
        },
        widget=forms.NumberInput(attrs={'min': 1, 'max': MAX_GUESTS}),
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

    def clean(self):
        cleaned = super().clean()
        guests = cleaned.get('guests')
        table = cleaned.get('table')
        # A single picked table can only hold 6. Bigger parties are given several
        # tables by the view, so the one hidden table is ignored for them.
        if guests and table and guests > SEATS_PER_TABLE:
            cleaned['table'] = None
        return cleaned


class TableForm(forms.ModelForm):
    class Meta:
        model = Table
        fields = ['number', 'capacity']
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
    """Customer booking form.

    CHANGED: the customer no longer picks a table. The booking is saved as
    'pending' with no table, and the admin chooses the table(s) when approving.
    """

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
        fields = ['name', 'phone', 'guests', 'booking_date', 'booking_time',
                  'occasion', 'special_request']
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
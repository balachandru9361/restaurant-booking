import re
from django import forms
from django.utils import timezone
from .models import TableBooking, Table

# One table seats 6 guests. A bigger party gets several tables.
SEATS_PER_TABLE = 6
MAX_GUESTS = 30


def tables_needed(guests):
    """How many tables a party of this size needs (7 guests -> 2 tables)."""
    return -(-int(guests) // SEATS_PER_TABLE)


class TableBookingForm(forms.ModelForm):
    """Customer booking form.

    The customer does not pick a table. The booking is saved as
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Past dates cannot be picked in the date box.
        # Uses the local (TIME_ZONE) date, not the server's UTC date.
        self.fields['booking_date'].widget.attrs['min'] = timezone.localdate().isoformat()

    def clean_phone(self):
        phone = (self.cleaned_data.get('phone') or '').strip()
        # Indian mobile numbers: exactly 10 digits, starting with 6, 7, 8, or 9
        if not re.match(r'^[6-9]\d{9}$', phone):
            raise forms.ValidationError(
                'Please enter a valid 10-digit Indian mobile number.'
            )
        return phone

    # A booking cannot be for a past date
    def clean_booking_date(self):
        booking_date = self.cleaned_data.get('booking_date')
        if booking_date and booking_date < timezone.localdate():
            raise forms.ValidationError('Please choose today or a future date.')
        return booking_date

    # For a booking today, the time must not be in the past
    def clean(self):
        cleaned = super().clean()
        booking_date = cleaned.get('booking_date')
        booking_time = cleaned.get('booking_time')
        if booking_date and booking_time and booking_date == timezone.localdate():
            if booking_time <= timezone.localtime().time():
                self.add_error(
                    'booking_time',
                    'That time has already passed today. Please choose a later time.'
                )
        return cleaned


class TableForm(forms.ModelForm):
    """Admin form for Add New Table / Edit Table.
    CHANGED: table_type added (Normal Table for walk-ins, Booking Table for reservations).
    A new table starts as a Normal Table unless the admin chooses otherwise."""

    class Meta:
        model = Table
        fields = ['number', 'capacity', 'table_type']
        labels = {
            'number': 'Table Number',
            'capacity': 'Seat Capacity',
            'table_type': 'Table Type',
        }
        widgets = {
            'table_type': forms.Select(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Show the two types without a blank "---------" choice
        self.fields['table_type'].choices = Table.TYPE_CHOICES
        if not self.instance.pk:
            self.fields['table_type'].initial = Table.NORMAL
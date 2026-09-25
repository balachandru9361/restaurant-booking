import logging
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse
from django.db import transaction
from django.core.mail import send_mail
from django.conf import settings
from .forms import TableBookingForm
from .models import TableBooking, Table
from .availability import (
    booking_window, busy_table_ids_for_slot, free_tables_for_slot,
)

logger = logging.getLogger(__name__)


def _parse_slot(date_str, time_str):
    """'2026-09-20' + '19:30' -> aware datetime, or None if it can't be parsed."""
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        t = datetime.strptime(time_str[:5], '%H:%M').time()
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(datetime.combine(d, t))


@login_required
def book_table(request):
    if request.method == 'POST':
        form = TableBookingForm(request.POST)
        if form.is_valid():
            booking = form.save(commit=False)

            # Hard cap: max 6 guests per table booking
            if booking.guests > 6:
                messages.error(request, 'Max 6 guests allowed per table. Please choose 6 or fewer.')
                return render(request, 'booking/booking_form.html', {'form': form})

            # The JS table-grid sends the table the customer clicked (optional).
            selected_table = form.cleaned_data.get('table')

            # Server-side availability double-check + assign a table.
            # A booked table is held for 2 hours from the booked time.
            with transaction.atomic():
                slot_start = timezone.make_aware(
                    datetime.combine(booking.booking_date, booking.booking_time)
                )
                free_tables = free_tables_for_slot(slot_start, booking.guests)

                if not free_tables:
                    messages.error(
                        request,
                        'Sorry, no table is free for this time slot. Please choose another time or reduce guests.'
                    )
                    return render(request, 'booking/booking_form.html', {'form': form})

                if selected_table:
                    # Re-check the customer's chosen table is still free and big
                    # enough - someone else may have booked it in the meantime.
                    if selected_table not in free_tables:
                        messages.error(
                            request,
                            f'Sorry, Table #{selected_table.number} was just booked or is too small. '
                            'Please pick another table.'
                        )
                        return render(request, 'booking/booking_form.html', {'form': form})
                    booking.table = selected_table
                else:
                    booking.table = free_tables[0]  # best fit: smallest table that fits the group

                booking.user = request.user
                booking.save()

            # --- Send booking confirmation email (never fail the booking if mail is down) ---
            if request.user.email:
                try:
                    send_mail(
                        subject=f'Table Booking Confirmed - {booking.booking_date}',
                        message=(
                            f"Hi {booking.name},\n\n"
                            f"Your table booking is confirmed!\n\n"
                            f"Date: {booking.booking_date}\n"
                            f"Time: {booking.booking_time.strftime('%I:%M %p')}\n"
                            f"Guests: {booking.guests}\n"
                            f"Table: #{booking.table.number} (held for 2 hours)\n"
                            f"Occasion: {booking.get_occasion_display()}\n\n"
                            f"We look forward to hosting you at Chandru Restaurant!"
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[request.user.email],
                        fail_silently=False,
                    )
                except Exception:
                    logger.exception('Could not send booking email for booking %s', booking.id)

            messages.success(request, f'Table #{booking.table.number} booked successfully! It is reserved for you for 2 hours.')
            request.session['last_booking_id'] = booking.id
            return redirect('booking_success')
    else:
        form = TableBookingForm()

    return render(request, 'booking/booking_form.html', {'form': form})


@login_required
def check_availability(request):
    """AJAX endpoint: is a table free for this date + time + guests?
    A booking holds a table for 2 hours, so slots that overlap are checked too."""
    date = request.GET.get('date')
    time = request.GET.get('time')
    guests = request.GET.get('guests', 1)

    try:
        guests = int(guests)
    except (TypeError, ValueError):
        guests = 1

    if not date or not time:
        return JsonResponse({'available': False, 'message': 'Date and time required'})

    # Hard cap: max 6 guests per table booking
    if guests > 6:
        return JsonResponse({
            'available': False,
            'message': '⚠️ Max 6 guests allowed per table. Please choose 6 or fewer, or split into multiple bookings.'
        })

    slot_start = _parse_slot(date, time)
    if slot_start is None:
        return JsonResponse({'available': False, 'message': 'Invalid date or time'})

    free_tables = free_tables_for_slot(slot_start, guests)
    free_seats = sum(t.capacity for t in free_tables)

    if not free_tables:
        return JsonResponse({
            'available': False,
            'remaining': 0,
            'message': '❌ No table free for this time (each booking holds a table for 2 hours). Try another slot.'
        })

    n = len(free_tables)
    return JsonResponse({
        'available': True,
        'remaining': free_seats,
        'message': f'✅ Available! {n} table{"s" if n != 1 else ""} free for this slot (held for 2 hours once booked).'
    })


@login_required
def table_status_for_slot(request):
    """AJAX endpoint: per-table free/booked status for a given date + time
    (+ optional guests, to flag tables too small for the party), so the
    booking page can render a table grid like the cart page does."""
    date = request.GET.get('date')
    time = request.GET.get('time')
    guests = request.GET.get('guests', 1)

    try:
        guests = int(guests)
    except (TypeError, ValueError):
        guests = 1

    slot_start = _parse_slot(date, time)
    if slot_start is None:
        return JsonResponse({'tables': []})

    busy_ids = busy_table_ids_for_slot(slot_start)

    tables = Table.objects.all().order_by('number')
    data = [
        {
            'id': t.id,
            'number': t.number,
            'capacity': t.capacity,
            'busy': t.id in busy_ids,
            'fits': t.capacity >= guests,
        }
        for t in tables
    ]
    return JsonResponse({'tables': data})


@login_required
def bookings_for_date(request):
    """AJAX endpoint: bookings (with their 2-hour window) for a given date, plus
    how many seats are held at the chosen time (or right now, for today)."""
    date = request.GET.get('date')
    time = request.GET.get('time')

    if not date:
        return JsonResponse({
            'bookings': [],
            'tables': [],
            'total_capacity': 0,
            'booked_guests': 0,
            'remaining': 0,
        })

    bookings = TableBooking.objects.filter(booking_date=date).order_by('booking_time')

    data = []
    for b in bookings:
        start, end = booking_window(b)
        data.append({
            'name': b.name,
            'time': b.booking_time.strftime('%I:%M %p'),
            'until': timezone.localtime(end).strftime('%I:%M %p'),
            'guests': b.guests,
            'occasion': b.get_occasion_display(),
            'opened': b.released,
        })

    tables = list(Table.objects.values('id', 'capacity'))
    total_capacity = sum(t['capacity'] for t in tables)

    # Seats held at the chosen time; if no time chosen, use "now" for today
    slot = _parse_slot(date, time) if time else None
    if slot is None and date == str(timezone.localdate()):
        slot = timezone.now()

    booked_seats = 0
    if slot is not None:
        busy_ids = busy_table_ids_for_slot(slot)
        booked_seats = sum(t['capacity'] for t in tables if t['id'] in busy_ids)

    return JsonResponse({
        'bookings': data,
        'tables': tables,
        'total_capacity': total_capacity,
        'booked_guests': booked_seats,
        'remaining': total_capacity - booked_seats,
    })


@login_required
def booking_success(request):
    booking_id = request.session.get('last_booking_id')
    booking = TableBooking.objects.filter(id=booking_id, user=request.user).first()
    return render(request, 'booking/booking_success.html', {'booking': booking})


@login_required
def booking_history(request):
    today = timezone.localdate()
    all_bookings = TableBooking.objects.filter(user=request.user)
    upcoming_bookings = all_bookings.filter(booking_date__gte=today)
    past_bookings = all_bookings.filter(booking_date__lt=today)

    context = {
        'upcoming_bookings': upcoming_bookings,
        'past_bookings': past_bookings,
    }
    return render(request, 'booking/booking_history.html', context)


@login_required
def cancel_booking(request, booking_id):
    booking = get_object_or_404(TableBooking, id=booking_id, user=request.user)

    if request.method == 'POST':
        booking.delete()
        messages.success(request, 'Your booking has been cancelled.')
        return redirect('booking_history')

    return render(request, 'booking/cancel_confirm.html', {'booking': booking})
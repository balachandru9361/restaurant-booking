import logging
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse
from django.core.mail import send_mail
from django.conf import settings
from .forms import TableBookingForm, SEATS_PER_TABLE, MAX_GUESTS, tables_needed
from .models import TableBooking, Table
from .availability import busy_table_ids_for_slot, free_tables_for_slot

logger = logging.getLogger(__name__)

# Bookings in these states no longer hold a table.
INACTIVE_STATUSES = ('rejected', 'cancelled')


def _parse_slot(date_str, time_str):
    """'2026-09-20' + '19:30' -> aware datetime, or None if it can't be parsed."""
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        t = datetime.strptime(time_str[:5], '%H:%M').time()
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(datetime.combine(d, t))


def _is_past_slot(slot_start):
    """True if the slot is already over (earlier than right now)."""
    return slot_start < timezone.now()


def _free_tables(slot_start, guests):
    """Free tables for this slot. Up to 6 guests: tables big enough for the party.
    7+ guests: every free table (the admin gives several tables together)."""
    if guests <= SEATS_PER_TABLE:
        return free_tables_for_slot(slot_start, guests)
    busy_ids = busy_table_ids_for_slot(slot_start)
    return [t for t in Table.objects.all().order_by('number') if t.id not in busy_ids]


def _enough_free_seats(free_tables, guests):
    """True if the free tables can seat the party. Up to 6 guests the tables are already
    filtered to ones big enough; for 7+ guests the admin may combine tables, so the total
    free seats must cover the guests (a single big table is enough too)."""
    if guests <= SEATS_PER_TABLE:
        return len(free_tables) >= 1
    return sum(t.capacity for t in free_tables) >= guests


@login_required
def book_table(request):
    if request.method == 'POST':
        form = TableBookingForm(request.POST)
        if form.is_valid():
            booking = form.save(commit=False)

            # Safety check (the form already limits this)
            if booking.guests > MAX_GUESTS:
                messages.error(request, f'Max {MAX_GUESTS} guests allowed per booking.')
                return render(request, 'booking/booking_form.html', {'form': form})

            if booking.guests < 1:
                messages.error(request, 'At least 1 guest is required.')
                return render(request, 'booking/booking_form.html', {'form': form})

            # Past date / past time is not allowed
            requested_slot = timezone.make_aware(
                datetime.combine(booking.booking_date, booking.booking_time)
            )
            if _is_past_slot(requested_slot):
                messages.error(
                    request,
                    'You cannot book a table for a past date or time. '
                    'Please choose a future date and time.'
                )
                return render(request, 'booking/booking_form.html', {'form': form})

            # The customer does not pick a table. We only check that enough
            # seats are free for this slot; the admin chooses the table(s)
            # when approving the request.
            if not _enough_free_seats(_free_tables(requested_slot, booking.guests), booking.guests):
                if booking.guests > SEATS_PER_TABLE:
                    msg = (f'Sorry, there are not enough free seats for {booking.guests} guests '
                           'at this time. Please choose another time or reduce guests.')
                else:
                    msg = ('Sorry, no table is free for this time slot. '
                           'Please choose another time or reduce guests.')
                messages.error(request, msg)
                return render(request, 'booking/booking_form.html', {'form': form})

            booking.user = request.user
            # No table yet: it stays empty until the admin approves and picks tables.
            booking.table = None
            booking.status = 'pending'
            booking.save()

            # --- Tell the customer the request was received (never fail the booking if mail is down) ---
            if request.user.email:
                try:
                    send_mail(
                        subject=f'Table Booking Request Received - {booking.booking_date}',
                        message=(
                            f"Hi {booking.name},\n\n"
                            f"We received your table booking request. It is waiting for admin approval.\n"
                            f"Your table will be confirmed by the admin. "
                            f"You will get another email as soon as it is approved.\n\n"
                            f"Date: {booking.booking_date}\n"
                            f"Time: {booking.booking_time.strftime('%I:%M %p')}\n"
                            f"Guests: {booking.guests}\n"
                            f"Occasion: {booking.get_occasion_display()}\n\n"
                            f"Chandru Restaurant"
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[request.user.email],
                        fail_silently=False,
                    )
                except Exception:
                    logger.exception('Could not send booking email for booking %s', booking.id)

            messages.success(request, 'Your booking request is sent. Waiting for admin approval.')
            request.session['last_booking_id'] = booking.id
            return redirect('booking_success')
    else:
        form = TableBookingForm()

    return render(request, 'booking/booking_form.html', {'form': form})


@login_required
def check_availability(request):
    """AJAX endpoint: are seats free for this date + time + guests?
    This is only a hint for the customer - the admin picks the real table(s),
    so for big parties we do not promise a number of tables."""
    date = request.GET.get('date')
    time = request.GET.get('time')
    guests = request.GET.get('guests', 1)

    try:
        guests = int(guests)
    except (TypeError, ValueError):
        guests = 1

    if not date or not time:
        return JsonResponse({'available': False, 'message': 'Date and time required'})

    if guests < 1:
        return JsonResponse({
            'available': False,
            'message': '⚠️ At least 1 guest is required.'
        })

    if guests > MAX_GUESTS:
        return JsonResponse({
            'available': False,
            'message': f'⚠️ Max {MAX_GUESTS} guests allowed per booking.'
        })

    slot_start = _parse_slot(date, time)
    if slot_start is None:
        return JsonResponse({'available': False, 'message': 'Invalid date or time'})

    if _is_past_slot(slot_start):
        return JsonResponse({
            'available': False,
            'message': '⚠️ This date/time is in the past. Please choose a future slot.'
        })

    big_party = guests > SEATS_PER_TABLE
    needed = tables_needed(guests)
    free_tables = _free_tables(slot_start, guests)
    free_seats = sum(t.capacity for t in free_tables)
    n = len(free_tables)

    if not _enough_free_seats(free_tables, guests):
        if not big_party:
            msg = '❌ No table free for this time. Try another slot.'
        else:
            msg = (f'❌ Not enough free seats for {guests} guests at this time '
                   f'({free_seats} seats free). Try another time or fewer guests.')
        return JsonResponse({
            'available': False,
            'remaining': free_seats,
            'tables_needed': needed,
            'message': msg,
        })

    if not big_party:
        msg = (f'✅ Available! {n} table{"s" if n != 1 else ""} free for this slot. '
               'Your table will be confirmed by the admin.')
    else:
        msg = (f'✅ Available! Our team will arrange seating for {guests} guests '
               'and confirm your booking.')
    return JsonResponse({
        'available': True,
        'remaining': free_seats,
        'tables_needed': needed,
        'message': msg,
    })


@login_required
def table_status_for_slot(request):
    """AJAX endpoint: per-table free/booked status for a given date + time.
    The customer no longer picks a table, but this stays so existing URLs and
    any admin/status widgets that call it keep working."""
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
    big_party = guests > SEATS_PER_TABLE

    tables = Table.objects.all().order_by('number')
    data = [
        {
            'id': t.id,
            'number': t.number,
            'capacity': t.capacity,
            'busy': t.id in busy_ids,
            'fits': True if big_party else t.capacity >= guests,
        }
        for t in tables
    ]
    return JsonResponse({
        'tables': data,
        'tables_needed': tables_needed(guests),
        'seats_per_table': SEATS_PER_TABLE,
    })


@login_required
def bookings_for_date(request):
    """AJAX endpoint: bookings for a given date, plus how many seats are held
    at the chosen time (or right now, for today).
    There is no 2-hour window any more, so no 'until' time is sent."""
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

    try:
        datetime.strptime(date, '%Y-%m-%d')
    except (TypeError, ValueError):
        return JsonResponse({'bookings': [], 'tables': [], 'total_capacity': 0,
                             'booked_guests': 0, 'remaining': 0})

    # Rejected and cancelled bookings are not shown (they do not hold a table)
    bookings = (TableBooking.objects.filter(booking_date=date)
                .exclude(status__in=INACTIVE_STATUSES)
                .prefetch_related('extra_tables')
                .order_by('booking_time'))

    data = []
    for b in bookings:
        data.append({
            'time': b.booking_time.strftime('%I:%M %p'),
            'until': '',
            'guests': b.guests,
            # 0 while the admin has not chosen tables yet
            'tables_count': (1 if b.table_id else 0) + len(b.extra_tables.all()),
            'opened': b.released,
            'status': b.status,
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

    # Already cancelled / rejected, or the table was already opened for the guests
    if booking.status in INACTIVE_STATUSES:
        messages.info(request, 'This booking is already closed.')
        return redirect('booking_history')
    if booking.released:
        messages.error(request, 'This booking is already in progress and cannot be cancelled.')
        return redirect('booking_history')

    if request.method == 'POST':
        # Keep the record: only change the status.
        booking.status = 'cancelled'
        booking.save(update_fields=['status'])
        messages.success(request, 'Your booking has been cancelled.')
        return redirect('booking_history')

    return render(request, 'booking/booking/cancel_confirm.html', {'booking': booking})
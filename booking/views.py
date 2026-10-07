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
from .forms import TableBookingForm, SEATS_PER_TABLE, MAX_GUESTS, tables_needed
from .models import TableBooking, Table
from .availability import (
    booking_window, busy_table_ids_for_slot, free_tables_for_slot, pick_tables_for_party,
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

            # Safety check (the form already limits this)
            if booking.guests > MAX_GUESTS:
                messages.error(request, f'Max {MAX_GUESTS} guests allowed per booking.')
                return render(request, 'booking/booking_form.html', {'form': form})

            # The JS table-grid sends the table the customer clicked (optional).
            # For parties above 6 the form ignores it (several tables are given instead).
            selected_table = form.cleaned_data.get('table')

            # Server-side availability double-check + assign tables.
            # A booked table is held for 2 hours from the booked time.
            with transaction.atomic():
                slot_start = timezone.make_aware(
                    datetime.combine(booking.booking_date, booking.booking_time)
                )

                if selected_table and booking.guests <= SEATS_PER_TABLE:
                    # Re-check the customer's chosen table is still free and big
                    # enough - someone else may have booked it in the meantime.
                    busy_ids = busy_table_ids_for_slot(slot_start)
                    if selected_table.id in busy_ids or selected_table.capacity < booking.guests:
                        messages.error(
                            request,
                            f'Sorry, Table #{selected_table.number} was just booked or is too small. '
                            'Please pick another table.'
                        )
                        return render(request, 'booking/booking_form.html', {'form': form})

                chosen = pick_tables_for_party(slot_start, booking.guests, preferred=selected_table)

                if not chosen:
                    if booking.guests > SEATS_PER_TABLE:
                        msg = (f'Sorry, {tables_needed(booking.guests)} tables are needed for '
                               f'{booking.guests} guests, but not enough tables are free for this time slot. '
                               'Please choose another time or reduce guests.')
                    else:
                        msg = ('Sorry, no table is free for this time slot. '
                               'Please choose another time or reduce guests.')
                    messages.error(request, msg)
                    return render(request, 'booking/booking_form.html', {'form': form})

                booking.user = request.user
                booking.table = chosen[0]
                # The booking waits for the admin. It is NOT confirmed yet.
                booking.status = 'pending'
                booking.save()
                booking.extra_tables.set(chosen[1:])

            # --- Tell the customer the request was received (never fail the booking if mail is down) ---
            if request.user.email:
                try:
                    table_line = ''
                    if len(chosen) > 1:
                        table_line = f"Tables reserved: {len(chosen)}\n"
                    send_mail(
                        subject=f'Table Booking Request Received - {booking.booking_date}',
                        message=(
                            f"Hi {booking.name},\n\n"
                            f"We received your table booking request. It is waiting for admin approval.\n"
                            f"You will get another email as soon as it is approved.\n\n"
                            f"Date: {booking.booking_date}\n"
                            f"Time: {booking.booking_time.strftime('%I:%M %p')}\n"
                            f"Guests: {booking.guests}\n"
                            f"{table_line}"
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
    """AJAX endpoint: are tables free for this date + time + guests?
    A booking holds a table for 2 hours, so slots that overlap are checked too.
    One table seats 6, so a bigger party needs several free tables."""
    date = request.GET.get('date')
    time = request.GET.get('time')
    guests = request.GET.get('guests', 1)

    try:
        guests = int(guests)
    except (TypeError, ValueError):
        guests = 1

    if not date or not time:
        return JsonResponse({'available': False, 'message': 'Date and time required'})

    if guests > MAX_GUESTS:
        return JsonResponse({
            'available': False,
            'message': f'⚠️ Max {MAX_GUESTS} guests allowed per booking.'
        })

    slot_start = _parse_slot(date, time)
    if slot_start is None:
        return JsonResponse({'available': False, 'message': 'Invalid date or time'})

    needed = tables_needed(guests)

    if guests <= SEATS_PER_TABLE:
        free_tables = free_tables_for_slot(slot_start, guests)
    else:
        busy_ids = busy_table_ids_for_slot(slot_start)
        free_tables = [t for t in Table.objects.all().order_by('number') if t.id not in busy_ids]

    free_seats = sum(t.capacity for t in free_tables)
    n = len(free_tables)

    if n < needed:
        if needed == 1:
            msg = '❌ No table free for this time (each booking holds a table for 2 hours). Try another slot.'
        else:
            msg = (f'❌ {guests} guests need {needed} tables (one table seats {SEATS_PER_TABLE}), '
                   f'but only {n} free for this slot. Try another time or fewer guests.')
        return JsonResponse({
            'available': False,
            'remaining': free_seats,
            'tables_needed': needed,
            'message': msg,
        })

    if needed == 1:
        msg = (f'✅ Available! {n} table{"s" if n != 1 else ""} free for this slot. '
               'Your booking will be confirmed after admin approval.')
    else:
        msg = (f'✅ Available! {guests} guests will get {needed} tables '
               f'(one table seats {SEATS_PER_TABLE}). '
               'Your booking will be confirmed after admin approval.')
    return JsonResponse({
        'available': True,
        'remaining': free_seats,
        'tables_needed': needed,
        'message': msg,
    })


@login_required
def table_status_for_slot(request):
    """AJAX endpoint: per-table free/booked status for a given date + time
    (+ optional guests, to flag tables too small for the party), so the
    booking page can render a table grid like the cart page does.
    For parties above 6 guests every free table counts as fitting, because
    several tables are given together."""
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

    # Rejected bookings are not shown (they do not hold a table)
    bookings = (TableBooking.objects.filter(booking_date=date)
                .exclude(status='rejected')
                .prefetch_related('extra_tables')
                .order_by('booking_time'))

    data = []
    for b in bookings:
        start, end = booking_window(b)
        data.append({
            'name': b.name,
            'time': b.booking_time.strftime('%I:%M %p'),
            'until': timezone.localtime(end).strftime('%I:%M %p'),
            'guests': b.guests,
            'tables_count': 1 + len(b.extra_tables.all()),
            'occasion': b.get_occasion_display(),
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

    if request.method == 'POST':
        booking.delete()
        messages.success(request, 'Your booking has been cancelled.')
        return redirect('booking_history')

    return render(request, 'booking/cancel_confirm.html', {'booking': booking})
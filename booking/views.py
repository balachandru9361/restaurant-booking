from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse
from django.db.models import Sum
from django.core.mail import send_mail
from django.conf import settings
from .forms import TableBookingForm
from .models import TableBooking, Table


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

            # Server-side availability double-check before saving
            total_capacity = Table.objects.aggregate(total=Sum('capacity'))['total'] or 0
            booked_guests = TableBooking.objects.filter(
                booking_date=booking.booking_date,
                booking_time=booking.booking_time
            ).aggregate(total=Sum('guests'))['total'] or 0

            if booked_guests + booking.guests > total_capacity:
                messages.error(
                    request,
                    'Sorry, not enough seats available for this time slot. Please choose another time or reduce guests.'
                )
                return render(request, 'booking/booking_form.html', {'form': form})

            booking.user = request.user
            booking.save()

            # --- Send booking confirmation email ---
            if request.user.email:
                send_mail(
                    subject=f'Table Booking Confirmed - {booking.booking_date}',
                    message=(
                        f"Hi {booking.name},\n\n"
                        f"Your table booking is confirmed!\n\n"
                        f"Date: {booking.booking_date}\n"
                        f"Time: {booking.booking_time.strftime('%I:%M %p')}\n"
                        f"Guests: {booking.guests}\n"
                        f"Occasion: {booking.get_occasion_display()}\n\n"
                        f"We look forward to hosting you at Chandru Restaurant!"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[request.user.email],
                    fail_silently=False,
                )

            messages.success(request, 'Table booked successfully!')
            request.session['last_booking_id'] = booking.id
            return redirect('booking_success')
    else:
        form = TableBookingForm()

    return render(request, 'booking/booking_form.html', {'form': form})


@login_required
def check_availability(request):
    """AJAX endpoint: returns remaining seats for a given date + time."""
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

    total_capacity = Table.objects.aggregate(total=Sum('capacity'))['total'] or 0

    booked_guests = TableBooking.objects.filter(
        booking_date=date,
        booking_time=time
    ).aggregate(total=Sum('guests'))['total'] or 0

    remaining = total_capacity - booked_guests

    if remaining <= 0:
        return JsonResponse({
            'available': False,
            'remaining': 0,
            'message': '❌ Fully booked for this time. Try another slot.'
        })
    elif guests > remaining:
        return JsonResponse({
            'available': False,
            'remaining': remaining,
            'message': f'⚠️ Only {remaining} seats left. Reduce guests or pick another time.'
        })
    else:
        return JsonResponse({
            'available': True,
            'remaining': remaining,
            'message': f'✅ Available! {remaining} seats left for this slot.'
        })


@login_required
def bookings_for_date(request):
    """AJAX endpoint: returns list of bookings (with customer name) for a given date,
    plus the per-table seat capacity, for the sidebar display."""
    date = request.GET.get('date')

    if not date:
        return JsonResponse({
            'bookings': [],
            'tables': [],
            'total_capacity': 0,
            'booked_guests': 0,
            'remaining': 0,
        })

    bookings = TableBooking.objects.filter(booking_date=date).order_by('booking_time')

    data = [
        {
            'name': b.name,
            'time': b.booking_time.strftime('%I:%M %p'),
            'guests': b.guests,
            'occasion': b.get_occasion_display(),
        }
        for b in bookings
    ]

    tables = list(Table.objects.values('id', 'capacity'))

    total_capacity = Table.objects.aggregate(total=Sum('capacity'))['total'] or 0
    booked_guests = bookings.aggregate(total=Sum('guests'))['total'] or 0

    return JsonResponse({
        'bookings': data,
        'tables': tables,
        'total_capacity': total_capacity,
        'booked_guests': booked_guests,
        'remaining': total_capacity - booked_guests,
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
import json
import logging

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Sum
from django.core.mail import send_mail
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .decorators import admin_required, kitchen_access_required, kitchen_required, server_required
from .models import Notification
from accounts.forms import StaffCreationForm
from menu.models import Category, FoodItem, FoodItemImage
from menu.forms import MenuItemForm
from booking.models import Table, TableBooking
from booking.availability import booking_window, release_table, table_status_map, upcoming_status_map
from orders.models import Order

logger = logging.getLogger(__name__)

# Orders that have been paid for (used for sales totals). Sales must not drop
# to zero once the kitchen/server move an order past "confirmed".
PAID_STATUSES = ['confirmed', 'preparing', 'ready', 'delivered']


@admin_required
def admin_dashboard(request):
    context = {
        'total_menu_items': FoodItem.objects.count(),
        'total_tables': Table.objects.count(),
        'total_bookings': TableBooking.objects.count(),
        'total_users': User.objects.count(),
        'total_orders': Order.objects.count(),
        'total_sales': Order.objects.filter(status__in=PAID_STATUSES).aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
        'recent_orders': Order.objects.order_by('-created_at')[:5],
        'recent_bookings': TableBooking.objects.order_by('-created_at')[:5],
        'notifications': Notification.objects.select_related('order')[:10],
        'unread_notifications': Notification.objects.filter(is_read=False).count(),
    }
    return render(request, 'dashboard/admin_home.html', context)


# ---- Menu ----
@admin_required
def manage_menu(request):
    return render(request, 'dashboard/manage_menu.html', {'items': FoodItem.objects.all()})


@admin_required
def add_menu_item(request):
    form = MenuItemForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        item = form.save()
        for img in request.FILES.getlist('extra_images'):
            FoodItemImage.objects.create(food_item=item, image=img)
        return redirect('dashboard:manage_menu')
    return render(request, 'dashboard/menu_form.html', {'form': form})


@admin_required
def edit_menu_item(request, pk):
    item = get_object_or_404(FoodItem, pk=pk)
    form = MenuItemForm(request.POST or None, request.FILES or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        item = form.save()
        for img in request.FILES.getlist('extra_images'):
            FoodItemImage.objects.create(food_item=item, image=img)
        return redirect('dashboard:manage_menu')
    return render(request, 'dashboard/menu_form.html', {'form': form, 'existing_images': item.gallery_images.all()})


@admin_required
def delete_menu_item(request, pk):
    get_object_or_404(FoodItem, pk=pk).delete()
    return redirect('dashboard:manage_menu')


# ---- Tables ----
@admin_required
def manage_tables(request):
    tables = Table.objects.all().order_by('number')
    holds = table_status_map()
    upcoming = upcoming_status_map()
    now = timezone.now()

    rows = []
    for table in tables:
        hold = holds.get(table.id)
        up = upcoming.get(table.id)
        rows.append({
            'table': table,
            'hold': hold,
            'mins_left': int((hold.end - now).total_seconds() // 60) + 1 if hold else 0,
            'upcoming': up,
            'upcoming_in_mins': int((up.start - now).total_seconds() // 60) + 1 if up else 0,
        })

    return render(request, 'dashboard/manage_tables.html', {
        'rows': rows,
        'tables': tables,
        'total_capacity': tables.aggregate(total=Sum('capacity'))['total'] or 0,
        'booked_count': len(holds),
        'free_count': tables.count() - len(holds),
        'upcoming_count': len(upcoming),
    })


@admin_required
@require_POST
def open_table(request, pk):
    """Admin 'Open Table' - lift the 2-hour hold before it expires."""
    table = get_object_or_404(Table, pk=pk)
    lifted = release_table(table.id)
    if lifted:
        messages.success(request, f'Table {table.number} is open again.')
    else:
        messages.info(request, f'Table {table.number} was already free.')
    nxt = request.POST.get('next')
    if nxt in ('manage_tables', 'manage_bookings'):
        return redirect(f'dashboard:{nxt}')
    return redirect('dashboard:manage_tables')


# ---- Bookings ----
@admin_required
def manage_bookings(request):
    bookings = list(
        TableBooking.objects.select_related('table').order_by('-booking_date', '-booking_time')
    )
    now = timezone.now()
    for b in bookings:
        start, end = booking_window(b)
        b.hold_end = end
        if b.released:
            b.hold_state = 'opened'
        elif now < start:
            b.hold_state = 'upcoming'
        elif now < end:
            b.hold_state = 'active'
        else:
            b.hold_state = 'completed'
    return render(request, 'dashboard/manage_bookings.html', {'bookings': bookings})


# ---- Users ----
@admin_required
def manage_users(request):
    users = User.objects.select_related('profile').all()
    return render(request, 'dashboard/manage_users.html', {'users': users})


@admin_required
def add_staff(request):
    form = StaffCreationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        role_label = user.profile.get_role_display()
        messages.success(request, f'{role_label} login created for "{user.username}".')
        return redirect('dashboard:manage_users')
    return render(request, 'dashboard/add_staff.html', {'form': form})


# ---- Orders ----
@admin_required
def manage_orders(request):
    orders = Order.objects.all().order_by('-created_at')
    return render(request, 'dashboard/manage_orders.html', {'orders': orders})


@admin_required
@require_POST
def mark_cash_received(request, pk):
    """Used by the 'Mark Received' button on Manage Orders for cash payments."""
    order = get_object_or_404(Order, pk=pk, payment_method='cash')
    order.payment_status = 'paid'
    order.save(update_fields=['payment_status'])
    return JsonResponse({'success': True})


# ---- Reports ----
@admin_required
def sales_reports(request):
    confirmed_orders = Order.objects.filter(status__in=PAID_STATUSES)

    total_orders = confirmed_orders.count()
    total_revenue = confirmed_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else 0

    recent_orders = confirmed_orders.order_by('-created_at')[:20]

    # Cash vs Online breakdown
    online_orders = confirmed_orders.filter(payment_method='online')
    cash_orders = confirmed_orders.filter(payment_method='cash')

    online_revenue = online_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    cash_revenue = cash_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    cash_pending_orders = confirmed_orders.filter(payment_method='cash', payment_status='cash_pending')
    cash_pending_amount = cash_pending_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    context = {
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'recent_orders': recent_orders,
        'online_revenue': online_revenue,
        'online_count': online_orders.count(),
        'cash_revenue': cash_revenue,
        'cash_count': cash_orders.count(),
        'cash_pending_amount': cash_pending_amount,
        'cash_pending_count': cash_pending_orders.count(),
    }
    return render(request, 'dashboard/sales_reports.html', context)


@admin_required
def add_table(request):
    from booking.forms import TableForm
    form = TableForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard:manage_tables')
    return render(request, 'dashboard/table_form.html', {'form': form})


@admin_required
def edit_table(request, pk):
    from booking.forms import TableForm
    table = get_object_or_404(Table, pk=pk)
    form = TableForm(request.POST or None, instance=table)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard:manage_tables')
    return render(request, 'dashboard/table_form.html', {'form': form})


@admin_required
def delete_table(request, pk):
    get_object_or_404(Table, pk=pk).delete()
    return redirect('dashboard:manage_tables')


# ---- QR Codes ----
@admin_required
def table_qr_codes(request):
    return render(request, 'dashboard/table_qr_codes.html', {
        'tables': Table.objects.all().order_by('number')
    })


# ---- Kitchen ----
@kitchen_required
def kitchen_dashboard(request):
    confirmed_orders = Order.objects.filter(status='confirmed').order_by('-created_at')
    preparing_orders = Order.objects.filter(status='preparing').order_by('-created_at')
    ready_orders = Order.objects.filter(status='ready').order_by('-created_at')

    context = {
        'confirmed_orders': confirmed_orders,
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
    }
    return render(request, 'dashboard/kitchen.html', context)

# Which status moves each role is allowed to make.
# Kitchen prepares; only the server marks an order as served (delivered).
ROLE_TRANSITIONS = {
    'kitchen': {('confirmed', 'preparing'), ('preparing', 'ready')},
    'server': {('ready', 'delivered')},
}


def _staff_role(user):
    if user.is_superuser:
        return 'admin'
    profile = getattr(user, 'profile', None)
    return profile.role if profile else None


def _board_for(role, new_status):
    if role == 'server':
        return 'dashboard:server_dashboard'
    if role == 'admin' and new_status == 'delivered':
        return 'dashboard:server_dashboard'
    return 'dashboard:kitchen_dashboard'


@kitchen_access_required
@require_POST
def update_order_status(request, pk, new_status):
    order = get_object_or_404(Order, pk=pk)
    role = _staff_role(request.user)

    valid_statuses = {value for value, _ in Order.STATUS_CHOICES}
    if new_status not in valid_statuses:
        messages.error(request, 'Invalid order status.')
        return redirect(_board_for(role, new_status))

    if role != 'admin' and (order.status, new_status) not in ROLE_TRANSITIONS.get(role, set()):
        if new_status == 'delivered':
            messages.error(request, 'Only the server can mark an order as served.')
        else:
            messages.error(request, f'Order #{order.id} cannot be moved to "{new_status}" from "{order.status}".')
        return redirect(_board_for(role, new_status))

    order.status = new_status
    order.save()

    if new_status == 'delivered':
        # Server served the table -> message to the admin dashboard
        where = f'Table {order.table.number}' if order.table else 'takeaway counter'
        Notification.objects.create(
            kind='served',
            order=order,
            created_by=request.user,
            message=f'Order #{order.id} served to {where} by {request.user.username}',
        )

        # Email the customer (a mail failure must not break the serve action)
        if order.user.email:
            try:
                send_mail(
                    subject=f'Order #{order.id} - Delivered',
                    message=(
                        f"Hi {order.user.get_full_name() or order.user.username},\n\n"
                        f"Your order has been delivered. Enjoy your meal! 🍽️\n\n"
                        f"Order #{order.id}\n"
                        f"Status: Delivered\n\n"
                        f"Thank you for choosing Chandru Restaurant!"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[order.user.email],
                    fail_silently=False,
                )
            except Exception:
                logger.exception('Could not send delivery email for order %s', order.id)

    return redirect(_board_for(role, new_status))


@admin_required
@require_POST
def api_order_status(request, pk):
    """Used by the status dropdown on Manage Orders (admin only)."""
    order = get_object_or_404(Order, pk=pk)
    try:
        status = json.loads(request.body).get('status')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Invalid data'}, status=400)

    if status not in {value for value, _ in Order.STATUS_CHOICES}:
        return JsonResponse({'ok': False, 'error': 'Invalid status'}, status=400)

    order.status = status
    order.save(update_fields=['status'])
    return JsonResponse({'ok': True, 'status': status})


# ---- Server ----
@server_required
def server_dashboard(request):
    ready_orders = Order.objects.filter(status='ready').select_related('table', 'user').order_by('created_at')
    delivered_today = Order.objects.filter(
        status='delivered', created_at__date=timezone.localdate()
    ).order_by('-created_at')
    todays_bookings = TableBooking.objects.filter(
        booking_date=timezone.localdate()
    ).select_related('table').order_by('booking_time')

    context = {
        'ready_orders': ready_orders,
        'delivered_today': delivered_today,
        'todays_bookings': todays_bookings,
    }
    return render(request, 'dashboard/server.html', context)


@server_required
def server_poll(request):
    """Polled by the server dashboard so a finished order shows up as a live message."""
    orders = Order.objects.filter(status='ready').select_related('table').order_by('created_at')
    return JsonResponse({
        'orders': [
            {'id': o.id, 'table': o.table.number if o.table else None}
            for o in orders
        ]
    })


# ---- Admin notifications (served orders) ----
@admin_required
def notifications_poll(request):
    latest = Notification.objects.first()
    return JsonResponse({
        'unread': Notification.objects.filter(is_read=False).count(),
        'latest_id': latest.id if latest else 0,
        'latest_message': latest.message if latest else '',
    })


@admin_required
@require_POST
def notifications_mark_read(request):
    Notification.objects.filter(is_read=False).update(is_read=True)
    return redirect('dashboard:admin_home')
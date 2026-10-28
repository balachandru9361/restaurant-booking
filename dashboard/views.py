import csv
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from django.http import HttpResponse
import json
import logging
from decimal import Decimal

try:
    import razorpay
except ImportError:          # library not installed: refunds fall back to manual
    razorpay = None

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum, F, Count, Q, Case, When, Value, IntegerField
from django.db.models import ProtectedError
from django.db.models.functions import TruncDate, TruncMonth
from django.core.mail import send_mail
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .decorators import admin_required, kitchen_required, server_required, store_required, store_or_kitchen_required
from .models import Notification, GroceryItem, RecipeIngredient
from accounts.forms import StaffCreationForm
from menu.models import Category, FoodItem, FoodItemImage
from menu.forms import MenuItemForm
from booking.models import Table, TableBooking
from booking.forms import SEATS_PER_TABLE, tables_needed
# CHANGED: later_reservations (clash with a later approved booking) and ALLOW_SINGLE_BIG_TABLE added
from booking.availability import (
    ALLOW_SINGLE_BIG_TABLE, is_table_busy_now, later_reservations,
    release_table, table_status_map, upcoming_status_map,
)
from orders.models import Order, OrderItem, Coupon

logger = logging.getLogger(__name__)

# Orders that have been paid for (used for sales totals). Sales must not drop
# to zero once the kitchen/server move an order past "confirmed".
PAID_STATUSES = ['confirmed', 'preparing', 'ready', 'delivered']


# ---- Pagination helpers ----
def _paginate(request, queryset, per_page=20):
    """Splits a queryset/list into pages using ?page=N.
    Returns (page_obj, querystring). querystring keeps every other GET param
    (status, q, date...) so filters do not disappear on page 2.
    It already ends with '&' (or is empty), so the template uses ?{{ querystring }}page=2"""
    paginator = Paginator(queryset, per_page)
    page_obj = paginator.get_page(request.GET.get('page'))  # bad/empty page -> page 1 or last page
    params = request.GET.copy()
    params.pop('page', None)
    encoded = params.urlencode()
    return page_obj, (encoded + '&') if encoded else ''


def _parse_date(raw):
    """'2026-10-10' -> date. Bad or empty input gives None instead of crashing."""
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


# ---- Sales charts data ----
def _sales_chart_data(start):
    """Data for the 4 charts on Sales / Reports.
    Daily = last 30 days, Monthly = last 12 months (always, so the trend is visible).
    Top / least foods follow the selected range (today / week / month / year / all)."""
    today = timezone.localdate()
    paid = Order.objects.filter(status__in=PAID_STATUSES)

    # ---- Daily sales: last 30 days (days with no orders show 0) ----
    d_start = today - timedelta(days=29)
    rows = (paid.filter(created_at__date__gte=d_start)
            .annotate(d=TruncDate('created_at'))
            .values('d')
            .annotate(total=Sum('total_amount'), n=Count('id')))
    by_day = {r['d']: r for r in rows}
    daily = {'labels': [], 'sales': [], 'orders': []}
    for i in range(30):
        day = d_start + timedelta(days=i)
        r = by_day.get(day)
        daily['labels'].append(day.strftime('%d %b'))
        daily['sales'].append(float(r['total']) if r else 0)
        daily['orders'].append(r['n'] if r else 0)

    # ---- Monthly sales: last 12 months ----
    first = today.replace(day=1)
    months = []
    m = first
    for _ in range(12):
        months.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    months.reverse()

    rows = (paid.filter(created_at__date__gte=months[0])
            .annotate(m=TruncMonth('created_at'))
            .values('m')
            .annotate(total=Sum('total_amount'), n=Count('id')))
    by_month = {}
    for r in rows:
        key = r['m'].date() if hasattr(r['m'], 'date') else r['m']
        by_month[key] = r
    monthly = {'labels': [], 'sales': [], 'orders': []}
    for mo in months:
        r = by_month.get(mo)
        monthly['labels'].append(mo.strftime('%b %Y'))
        monthly['sales'].append(float(r['total']) if r else 0)
        monthly['orders'].append(r['n'] if r else 0)

    # ---- Best / least selling foods (selected range) ----
    items = OrderItem.objects.filter(order__status__in=PAID_STATUSES)
    if start:
        items = items.filter(order__created_at__date__gte=start)
    ranked = items.values('food_item__name').annotate(qty=Sum('quantity'))
    top = list(ranked.order_by('-qty', 'food_item__name')[:5])
    least = list(ranked.order_by('qty', 'food_item__name')[:5])

    return {
        'daily': daily,
        'monthly': monthly,
        'top_foods': {
            'labels': [r['food_item__name'] for r in top],
            'qty': [int(r['qty']) for r in top],
        },
        'least_foods': {
            'labels': [r['food_item__name'] for r in least],
            'qty': [int(r['qty']) for r in least],
        },
    }


def _store_stock_overview():
    """Returns (all_items, low_items) from the store stock (the stock the recipes use).
    low_items = items that are running low or out, out of stock first.
    Each item also gets .quantity and .low_stock_limit so the old templates keep working."""
    from store.models import GroceryItem as StoreGrocery
    qs = StoreGrocery.objects.all()
    if any(f.name == 'is_active' for f in StoreGrocery._meta.get_fields()):
        qs = qs.filter(is_active=True)
    items = list(qs.order_by('name'))
    for g in items:
        try:
            g.quantity = g.current_stock
            g.low_stock_limit = g.reorder_level
        except AttributeError:
            pass
    low = [g for g in items if g.is_low or g.is_out]
    low.sort(key=lambda g: (not g.is_out, g.name))
    return items, low


@admin_required
def admin_dashboard(request):
    store_items, store_low = _store_stock_overview()
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
        'total_grocery': len(store_items),
        'low_stock_count': len(store_low),
        'low_stock_items': store_low[:5],
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
@require_POST
def delete_menu_item(request, pk):
    """POST only. A dish that was ordered before cannot be deleted (it would
    break old orders and sales reports), so it is hidden instead."""
    item = get_object_or_404(FoodItem, pk=pk)
    name = item.name
    try:
        with transaction.atomic():
            item.delete()
        messages.success(request, f'"{name}" deleted.')
    except ProtectedError:
        item.is_available = False
        item.save(update_fields=['is_available'])
        messages.warning(
            request,
            f'"{name}" is in past orders, so it cannot be deleted. '
            'It has been marked Not available instead.'
        )
    return redirect('dashboard:manage_menu')


# ---- Tables ----
@admin_required
def manage_tables(request):
    tables = Table.objects.all().order_by('number')
    holds = table_status_map()
    upcoming = upcoming_status_map()
    now = timezone.now()

    # CHANGED: no countdown any more (mins_left removed). A table that is held stays
    # held until the admin clicks "Open Table". Only the "starts in N minutes" hint is kept.
    rows = []
    for table in tables:
        hold = holds.get(table.id)
        up = upcoming.get(table.id)
        rows.append({
            'table': table,
            'hold': hold,
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
    """Admin 'Open Table' - free the table. There is no timer: a held table stays
    held until the admin clicks this. For a multi-table booking it opens all its tables."""
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
# A booking holds its table(s) from the booked time until the admin clicks "Open Table".
# There is no auto-expiry. After OVERDUE_AFTER the booking only shows a reminder.
OVERDUE_AFTER = timedelta(hours=3)


def _booking_start(booking):
    """Aware datetime of the booked slot (date + time are entered in local time)."""
    return timezone.make_aware(datetime.combine(booking.booking_date, booking.booking_time))


def _booking_tables(booking):
    """All tables of a booking (main table + extra tables), sorted by number.
    Empty list while the admin has not chosen a table yet.
    CHANGED: reads TableBooking.all_tables, so the admin pages, the customer pass and the
    server dashboard all get the tables from ONE place."""
    return booking.all_tables


def _tables_text(tables):
    """[1, 2, 5] -> 'Tables 1, 2, 5'. Empty -> 'No table yet'."""
    if not tables:
        return 'No table yet'
    numbers = ', '.join(str(t.number) for t in tables)
    return f'Table {numbers}' if len(tables) == 1 else f'Tables {numbers}'


def _slot_conflicts(booking):
    """{table_id: 'Held by ...'} - tables that clash with this booking.
    Two kinds of clash (both are only a warning for the admin, who can still choose such a table):
      1. still held by something that started before (or at) this booking's time
         and has not been opened by the admin yet.
      2. CHANGED: already reserved by a LATER approved booking the same day
         (example: this booking is 4:00 PM, Table 1 is approved for 4:50 PM).
         This booking keeps its tables until the admin opens them, so the later one would clash."""
    d, t = booking.booking_date, booking.booking_time
    conflicts = {}

    earlier = (TableBooking.objects
               .filter(status='approved', released=False,
                       booking_date__gte=d - timedelta(days=1), booking_date__lte=d)
               .filter(Q(booking_date__lt=d) | Q(booking_time__lte=t))
               .exclude(pk=booking.pk)
               .select_related('table')
               .prefetch_related('extra_tables'))
    for other in earlier:
        label = f'Held by booking #{other.pk} ({other.booking_time.strftime("%I:%M %p")})'
        for tb in _booking_tables(other):
            conflicts.setdefault(tb.id, label)

    # CHANGED: tables reserved for a later approved booking on the same day
    later = later_reservations(_booking_start(booking), exclude_booking_id=booking.pk)
    for table_id, hold in later.items():
        when = timezone.localtime(hold.start).strftime('%I:%M %p')
        conflicts.setdefault(table_id, f'Reserved for later booking #{hold.obj.pk} ({when})')

    # Running orders only matter when the booking is for today (or already past)
    if d <= timezone.localdate():
        running = (Order.objects
                   .filter(table__isnull=False, table_released=False,
                           created_at__gte=timezone.now() - timedelta(hours=24))
                   .exclude(status='cancelled'))
        for o in running:
            conflicts.setdefault(o.table_id, f'Held by order #{o.pk}')
    return conflicts


def _suggest_table_ids(guests, conflicts, tables_by_capacity):
    """Pre-selected tables for the approve screen: free tables only.
    Up to 6 guests = the smallest free table that fits.
    7+ guests = ONE free table when it has enough seats for the whole party
    (example: a 7 seat table for 7 guests, see ALLOW_SINGLE_BIG_TABLE), otherwise as many
    free tables as needed (full 6-seat tables first). Returns [] if there are not enough free tables."""
    free = [t for t in tables_by_capacity if t.id not in conflicts]
    if guests <= SEATS_PER_TABLE:
        for t in free:
            if t.capacity >= guests:
                return [t.id]
        return []

    # CHANGED: one table is enough when its seats cover the whole party
    if ALLOW_SINGLE_BIG_TABLE:
        for t in free:                       # free is already smallest-capacity first
            if t.capacity >= guests:
                return [t.id]

    needed = tables_needed(guests)
    full = [t for t in free if t.capacity >= SEATS_PER_TABLE]
    small = [t for t in free if t.capacity < SEATS_PER_TABLE]
    chosen = (full + small)[:needed]
    return [t.id for t in chosen] if len(chosen) >= needed else []


@admin_required
def manage_bookings(request):
    """Bookings list with tabs (all / upcoming / past), status filter, name/phone search, 15 per page.
    URL examples: ?tab=upcoming&page=2   ?tab=past&status=approved   ?q=ravi"""
    today = timezone.localdate()
    qs = TableBooking.objects.select_related('table').prefetch_related('extra_tables')

    tab = request.GET.get('tab', 'all')
    if tab not in ('all', 'upcoming', 'past'):
        tab = 'all'

    status = request.GET.get('status', '').strip()
    if status:
        qs = qs.filter(status=status)

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    # Tab counts (respect the status filter and search, so the numbers match the list)
    counts = {
        'all': qs.count(),
        'upcoming': qs.filter(booking_date__gte=today).count(),
        'past': qs.filter(booking_date__lt=today).count(),
    }

    # Status counts for the top cards and status chips (ALL bookings, not filtered)
    by_status = dict(TableBooking.objects.values_list('status').annotate(n=Count('id')))
    status_counts = {
        'all': sum(by_status.values()),
        'pending': by_status.get('pending', 0),
        'approved': by_status.get('approved', 0),
        'rejected': by_status.get('rejected', 0),
        'cancelled': by_status.get('cancelled', 0),
    }

    if tab == 'upcoming':
        # Soonest first is easier to work with for upcoming bookings
        qs = qs.filter(booking_date__gte=today).order_by('booking_date', 'booking_time')
    elif tab == 'past':
        qs = qs.filter(booking_date__lt=today).order_by('-booking_date', '-booking_time')
    else:
        qs = qs.order_by('-booking_date', '-booking_time')

    page_obj, querystring = _paginate(request, qs, per_page=15)

    # Work out the hold state only for the 15 rows on this page.
    # list() first: attributes set on objects are lost if the queryset is read again.
    # The helper attributes start with ui_ so they never clash with model properties.
    bookings = list(page_obj.object_list)
    now = timezone.now()
    tables_by_capacity = list(Table.objects.all().order_by('capacity', 'number'))
    tables_by_number = sorted(tables_by_capacity, key=lambda t: t.number)

    for b in bookings:
        b.ui_tables = _booking_tables(b)
        b.ui_table_text = _tables_text(b.ui_tables)
        b.ui_seats = sum(t.capacity for t in b.ui_tables)
        b.ui_options = []

        # CHANGED: hold_state has no timer. States:
        # pending / rejected / cancelled / opened / upcoming / active / overdue / no_table
        if b.status == 'pending':
            b.hold_state = 'pending'
        elif b.status == 'rejected':
            b.hold_state = 'rejected'
        elif b.status == 'cancelled':
            b.hold_state = 'cancelled'      # cancelled by the customer, holds no table
        elif b.released:
            b.hold_state = 'opened'
        elif not b.ui_tables:
            b.hold_state = 'no_table'
        else:
            start = _booking_start(b)
            if now < start:
                b.hold_state = 'upcoming'
            elif now >= start + OVERDUE_AFTER:
                b.hold_state = 'overdue'     # still held, time passed - only a reminder
            else:
                b.hold_state = 'active'

        # Pending bookings: build the table choices for the approve screen
        if b.status == 'pending':
            conflicts = _slot_conflicts(b)
            if b.ui_tables:
                suggested = {t.id for t in b.ui_tables}      # tables chosen before (old bookings)
            else:
                suggested = set(_suggest_table_ids(b.guests, conflicts, tables_by_capacity))
            b.ui_options = [{
                'id': t.id,
                'number': t.number,
                'capacity': t.capacity,
                'conflict': conflicts.get(t.id, ''),
                'selected': t.id in suggested,
            } for t in tables_by_number]
            b.ui_tables_needed = tables_needed(b.guests)
    page_obj.object_list = bookings

    return render(request, 'dashboard/manage_bookings.html', {
        'bookings': page_obj,          # loop over this exactly like before
        'page_obj': page_obj,
        'querystring': querystring,
        'tab': tab,
        'status': status,
        'q': q,
        'counts': counts,
        'status_counts': status_counts,
        'seats_per_table': SEATS_PER_TABLE,
    })


# ---- Users ----
# Same role rules the template uses: admin > server > kitchen (staff) > customer
USER_ROLE_Q = {
    'admin': Q(is_superuser=True),
    'server': ~Q(is_superuser=True) & Q(profile__role='server'),
    'staff': ~Q(is_superuser=True) & ~Q(profile__role='server') & Q(is_staff=True),
    'customer': ~Q(is_superuser=True) & ~Q(profile__role='server') & Q(is_staff=False),
}
USER_ROLE_LABELS = {'admin': 'Admin', 'server': 'Server', 'staff': 'Kitchen', 'customer': 'Customer'}
USER_SORTS = {'user': 'username', 'email': 'email', 'status': 'is_active', 'joined': 'date_joined'}


def _user_role(u):
    if u.is_superuser:
        return 'admin'
    profile = getattr(u, 'profile', None)
    if profile is not None and getattr(profile, 'role', None) == 'server':
        return 'server'
    if u.is_staff:
        return 'staff'
    return 'customer'


@admin_required
def manage_users(request):
    """Users list, 20 per page. Filters, search and sort live in the URL.
    URL examples: ?role=server   ?state=inactive   ?q=ravi&sort=-joined&page=2   ?export=csv"""
    f_role = request.GET.get('role', 'all')
    if f_role not in USER_ROLE_Q:
        f_role = 'all'
    f_state = request.GET.get('state', '')
    if f_state not in ('active', 'inactive'):
        f_state = ''
    f_sort = request.GET.get('sort', '-joined')
    if f_sort.lstrip('-') not in USER_SORTS:
        f_sort = '-joined'
    q = request.GET.get('q', '').strip()

    users = User.objects.select_related('profile')
    if f_role != 'all':
        users = users.filter(USER_ROLE_Q[f_role])
    if f_state:
        users = users.filter(is_active=(f_state == 'active'))
    if q:
        users = users.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q)
            | Q(last_name__icontains=q) | Q(email__icontains=q))
    users = users.order_by(('-' if f_sort.startswith('-') else '') + USER_SORTS[f_sort.lstrip('-')], 'id')

    # CSV export: every user matching the current filters (not just one page)
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="users_%s.csv"' % timezone.localdate()
        writer = csv.writer(response)
        writer.writerow(['Name', 'Username', 'Email', 'Role', 'Status', 'Joined'])
        for u in users:
            writer.writerow([
                u.get_full_name() or u.username,
                u.username,
                u.email,
                USER_ROLE_LABELS[_user_role(u)],
                'Active' if u.is_active else 'Inactive',
                timezone.localtime(u.date_joined).strftime('%d %b %Y'),
            ])
        return response

    # Stat cards: one query over ALL users (not only this page)
    agg = {'total': Count('id'), 'active': Count('id', filter=Q(is_active=True))}
    for key, cond in USER_ROLE_Q.items():
        agg[key] = Count('id', filter=cond)
    st = User.objects.aggregate(**agg)

    page_obj, querystring = _paginate(request, users, per_page=20)

    return render(request, 'dashboard/manage_users.html', {
        'users': page_obj,             # loop over this exactly like before
        'page_obj': page_obj,
        'querystring': querystring,
        'st': st,
        'f_role': f_role,
        'f_state': f_state,
        'f_sort': f_sort,
        'f_q': q,
    })


@admin_required
@require_POST
def bulk_user_action(request):
    """Deactivate / activate the users ticked on Manage Users."""
    try:
        data = json.loads(request.body)
        ids = [int(i) for i in data.get('ids', [])]
        action = data.get('action')
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Invalid data'}, status=400)

    if action not in ('deactivate', 'activate'):
        return JsonResponse({'ok': False, 'error': 'Invalid action'}, status=400)
    if not ids:
        return JsonResponse({'ok': False, 'error': 'No users selected'}, status=400)

    # Never lock yourself out, and never touch superusers
    users = User.objects.filter(id__in=ids).exclude(id=request.user.id).exclude(is_superuser=True)
    count = users.update(is_active=(action == 'activate'))

    skipped = len(ids) - count
    return JsonResponse({'ok': True, 'changed': count, 'skipped': skipped})


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
# A served (delivered) order keeps its table until the server taps
# "Customer Left" (table_released=True). Only cancelled orders free the table by themselves.
def _order_holding_table(table_id, exclude_order_id=None):
    """Active order that currently holds this table."""
    qs = (Order.objects.filter(table_id=table_id, table_released=False)
          .exclude(status='cancelled'))
    if exclude_order_id:
        qs = qs.exclude(pk=exclude_order_id)
    return qs.first()


# One place for the table rules (used by admin assign_table and server send-to-kitchen)
def _table_error(order, table):
    """Returns an error message if this order cannot take this table, else None."""
    other = _order_holding_table(table.id, exclude_order_id=order.id)
    if other:
        return f'Table #{table.number} is already given to order #{other.id}.'
    if order.table_id != table.id and is_table_busy_now(table.id):
        return f'Table #{table.number} is booked right now.'
    return None


# Tables that are free right now (not booked, not held by an order)
def _free_tables():
    held = set(
        Order.objects.filter(table__isnull=False, table_released=False)
        .exclude(status='cancelled')
        .values_list('table_id', flat=True)
    )
    return [t for t in Table.objects.all().order_by('number')
            if t.id not in held and not is_table_busy_now(t.id)]


# ---- Manage Orders: filters, stats, sorting (all done in the database) ----
KITCHEN_STATUSES = ['confirmed', 'preparing', 'ready']

_UNPAID = Q(payment_status='pending')
_CASH = Q(payment_method='cash')
_CANCELLED_PAID = Q(status='cancelled', payment_status='paid')

# Same rules the template used in JS, now as database filters
ORDER_FILTER_Q = {
    'approval': Q(status='pending') & ~Q(payment_status='pending'),
    'pending': Q(status='pending'),
    'kitchen': Q(status__in=KITCHEN_STATUSES),
    'delivered': Q(status__in=['delivered', 'completed']),
    'cancelled': Q(status='cancelled'),
}
ORDER_PAY_Q = {
    'unpaid': _UNPAID,
    'cashpaid': ~_UNPAID & _CASH & Q(payment_status='paid'),
    # CHANGED: a cancelled cash order has nothing to collect, so it is not "cash pending"
    'cashpending': ~_UNPAID & _CASH & ~Q(payment_status='paid') & ~Q(status='cancelled'),
    'refundpending': ~_UNPAID & ~_CASH & _CANCELLED_PAID & Q(refunded=False),
    'refunded': ~_UNPAID & ~_CASH & _CANCELLED_PAID & Q(refunded=True),
    'online': ~_UNPAID & ~_CASH & ~_CANCELLED_PAID,
}


@admin_required
def manage_orders(request):
    """Orders list, 20 per page. Filters live in the URL so they survive page 2, 3...
    URL examples: ?filter=approval   ?pay=cashpending   ?q=ravi&sort=amount&page=2"""
    f_filter = request.GET.get('filter', 'all')
    if f_filter not in ORDER_FILTER_Q:
        f_filter = 'all'
    f_pay = request.GET.get('pay', 'all')
    if f_pay not in ORDER_PAY_Q:
        f_pay = 'all'
    f_sort = request.GET.get('sort', 'attention')
    if f_sort not in ('attention', 'newest', 'oldest', 'amount'):
        f_sort = 'attention'
    q = request.GET.get('q', '').strip().lstrip('#')

    # NEW: prepared_by is loaded too, so Manage Orders can show a "Prepared by" column
    orders = (Order.objects
              .select_related('user', 'table', 'prepared_by')
              .prefetch_related('items__food_item'))

    if f_filter != 'all':
        orders = orders.filter(ORDER_FILTER_Q[f_filter])
    if f_pay != 'all':
        orders = orders.filter(ORDER_PAY_Q[f_pay])
    if q:
        cond = (Q(user__username__icontains=q) | Q(user__first_name__icontains=q)
                | Q(user__last_name__icontains=q)
                | Q(id__in=OrderItem.objects.filter(food_item__name__icontains=q).values('order_id')))
        if q.isdigit():
            cond |= Q(id=int(q))
        orders = orders.filter(cond)

    if f_sort == 'attention':
        orders = orders.annotate(needs=Case(
            When(ORDER_FILTER_Q['approval'], then=Value(1)),
            default=Value(0), output_field=IntegerField(),
        )).order_by('-needs', '-created_at', '-id')
    elif f_sort == 'oldest':
        orders = orders.order_by('created_at', 'id')
    elif f_sort == 'amount':
        orders = orders.order_by('-total_amount', '-id')
    else:
        orders = orders.order_by('-created_at', '-id')

    # CSV export: every order matching the current filters (not just one page)
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="orders_%s.csv"' % timezone.localdate()
        writer = csv.writer(response)
        writer.writerow(['Order', 'Customer', 'Table', 'Items', 'Amount', 'Payment', 'Payment Status', 'Status', 'Placed', 'Prepared by'])
        for o in orders:
            if o.table:
                where = f'Table {o.table.number}'
            elif o.is_takeaway:
                where = 'Takeaway'
            else:
                where = 'No table yet'
            writer.writerow([
                o.id,
                o.user.get_full_name() or o.user.username,
                where,
                '; '.join(f'{i.quantity} {i.food_item.name}' for i in o.items.all()),
                o.total_amount,
                o.payment_method,
                o.payment_status,
                o.status,
                timezone.localtime(o.created_at).strftime('%d %b %Y %H:%M'),
                (o.prepared_by.get_full_name() or o.prepared_by.username) if o.prepared_by else '',
            ])
        return response

    # Stat cards and chip counts: one query over ALL orders (not only this page)
    agg = {'total': Count('id')}
    for key, cond in ORDER_FILTER_Q.items():
        agg[key] = Count('id', filter=cond)
    for key, cond in ORDER_PAY_Q.items():
        agg['p_' + key] = Count('id', filter=cond)
    agg['sales'] = Sum('total_amount', filter=Q(created_at__date=timezone.localdate(), status__in=PAID_STATUSES))
    agg['cash'] = Sum('total_amount', filter=ORDER_PAY_Q['cashpending'] & ~Q(status='cancelled'))
    agg['refund'] = Sum('total_amount', filter=ORDER_PAY_Q['refundpending'])
    st = Order.objects.aggregate(**agg)
    for key in ('sales', 'cash', 'refund'):
        st[key] = st[key] or Decimal('0')

    page_obj, querystring = _paginate(request, orders, per_page=20)

    # ---- NEW: one combined row per running table ----
    # Orders of the same running table (customer not left yet) become ONE row:
    # one combined amount, one cash button. Example: Table 8 with #17 and #18 -> one row, Rs.4800.
    # Every row gets: sitting_orders, sitting_count, sitting_total, sitting_cash_ids,
    # sitting_cash_total, sitting_same_status, sitting_needs_approval, sitting_chefs, sitting_first.
    _page_orders = list(page_obj.object_list)
    _live = lambda o: bool(o.table_id) and not o.table_released and o.status != 'cancelled'
    _tids = {o.table_id for o in _page_orders if _live(o)}
    _by_table = {}
    if _tids:
        _running = (Order.objects
                    .filter(table_id__in=_tids, table_released=False)
                    .exclude(status='cancelled')
                    .select_related('user', 'prepared_by', 'table')
                    .prefetch_related('items__food_item')
                    .order_by('-id'))
        for g in _running:
            _by_table.setdefault(g.table_id, []).append(g)

    _rows = []
    _seen_sittings = set()
    for o in _page_orders:
        members = _by_table.get(o.table_id) if _live(o) else None
        if members and len(members) > 1:
            if o.table_id in _seen_sittings:
                continue                      # already shown inside the combined row
            _seen_sittings.add(o.table_id)
        else:
            members = [o]

        cash_due = [m for m in members
                    if m.payment_method == 'cash'
                    and m.status != 'cancelled'
                    and m.payment_status not in ('pending', 'paid')]
        chefs = []
        for m in members:
            if m.prepared_by_id:
                name = m.prepared_by.get_full_name() or m.prepared_by.username
                if name not in chefs:
                    chefs.append(name)

        o.sitting_orders = members
        o.sitting_count = len(members)
        o.sitting_total = sum((m.total_amount for m in members), Decimal('0'))
        o.sitting_cash_ids = [m.id for m in cash_due]
        o.sitting_cash_total = sum((m.total_amount for m in cash_due), Decimal('0'))
        o.sitting_same_status = len({(m.status, m.payment_status) for m in members}) == 1
        o.sitting_needs_approval = any(m.status == 'pending' and m.payment_status != 'pending'
                                       for m in members)
        o.sitting_chefs = chefs
        o.sitting_first = min(m.created_at for m in members)
        _rows.append(o)
    page_obj.object_list = _rows

    # ---- Table info (same as before) ----
    tables = list(Table.objects.all().order_by('number'))
    held = {
        o.table_id: o.id
        for o in Order.objects.filter(table__isnull=False, table_released=False)
                              .exclude(status='cancelled')
    }
    tables_info = [{
        'id': t.id,
        'number': t.number,
        'busy': is_table_busy_now(t.id),
        'held_by': held.get(t.id),
    } for t in tables]
    free_tables = [t for t in tables if not is_table_busy_now(t.id) and t.id not in held]

    return render(request, 'dashboard/manage_orders.html', {
        'orders': page_obj,            # loop over this exactly like before
        'page_obj': page_obj,
        'querystring': querystring,
        'st': st,
        'f_filter': f_filter,
        'f_pay': f_pay,
        'f_sort': f_sort,
        'f_q': q,
        'free_tables': free_tables,
        'tables_info': tables_info,
    })


@admin_required
@require_POST
def assign_table(request, pk):
    """Admin assigns (or clears) the table for an order from Manage Orders."""
    order = get_object_or_404(Order, pk=pk)
    table_id = request.POST.get('table', '').strip()

    if not table_id:
        order.table = None
        order.save(update_fields=['table'])
        return JsonResponse({'success': True, 'table': None})

    table = get_object_or_404(Table, pk=table_id)

    # Uses the shared table rules
    error = _table_error(order, table)
    if error:
        return JsonResponse({'success': False, 'error': error}, status=409)

    order.table = table
    order.table_released = False
    order.save(update_fields=['table', 'table_released'])
    return JsonResponse({'success': True, 'table': table.number})


@admin_required
@require_POST
def mark_cash_received(request, pk):
    """Used by the 'Mark Received' button on Manage Orders for cash payments.
    Cash received on an approved order also completes it (status -> delivered).
    A pending (not yet approved) order only gets the payment marked.
    A cancelled order has nothing to collect, so it is refused.
    NEW: a 'cash_received' Notification is saved so the Server dashboard can show
    a toast + sound as soon as the admin collects the cash."""
    order = get_object_or_404(Order, pk=pk, payment_method='cash')
    # CHANGED: never mark a rejected/cancelled order as cash paid
    if order.status == 'cancelled':
        return JsonResponse(
            {'success': False, 'error': f'Order #{order.id} is cancelled, so there is no cash to collect.'},
            status=400)
    order.payment_status = 'paid'
    fields = ['payment_status']
    if order.status in ('confirmed', 'preparing', 'ready'):
        order.status = 'delivered'
        fields.append('status')
    order.save(update_fields=fields)

    # NEW: tell the server dashboard (is_read=True so the admin unread badge is not changed)
    where = f'Table {order.table.number}' if order.table_id else 'Takeaway'
    Notification.objects.create(
        kind='cash_received',
        order=order,
        created_by=request.user,
        is_read=True,
        message=f'Cash received for order #{order.id} ({where}): Rs.{order.total_amount}'[:255],
    )
    return JsonResponse({'success': True, 'status': order.status})


@admin_required
@require_POST
def mark_refunded(request, pk):
    """Used by the 'Mark Refunded' button on Manage Orders: the admin has paid the
    money back for a cancelled order that was paid online."""
    order = get_object_or_404(Order, pk=pk)
    if order.status != 'cancelled' or order.payment_method != 'online' or order.payment_status != 'paid':
        return JsonResponse({'success': False, 'error': 'No refund is due for this order.'}, status=400)
    order.refunded = True
    order.save(update_fields=['refunded'])
    return JsonResponse({'success': True})


# The approve logic lives in a helper so the SERVER can use it too.
def _approve_pending_order(pk, force=False, table_id=None):
    """pending -> confirmed (appears on the Kitchen board). Checks the store stock.
    Returns (payload_dict, http_status)."""
    with transaction.atomic():
        # Lock the order row so a double click (or two staff) cannot use up the stock twice
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
        if order.status != 'pending':
            return {'ok': False, 'error': f'Order #{order.id} is already {order.status}.'}, 409
        if order.payment_status == 'pending':
            return {'ok': False, 'error': 'Customer has not completed payment yet.'}, 409

        fields = ['status']

        # CHANGED: the table is optional now. It normally comes from the customer's QR scan.
        # If a table is passed explicitly (admin override), it is assigned.
        # No table = no table badge, and the order is still sent to the kitchen.
        if table_id:
            table = Table.objects.filter(pk=table_id).first()
            if not table:
                return {'ok': False, 'error': 'Table not found.'}, 400
            table_err = _table_error(order, table)
            if table_err:
                return {'ok': False, 'error': table_err}, 409
            order.table = table
            order.table_released = False
            fields += ['table', 'table_released']

        # Check the store stock against each dish's store recipe.
        # The stock itself is used up when the kitchen presses Start Preparing
        # (see store/signals.py), so it is not touched here.
        from store.models import RecipeIngredient as StoreRecipe
        need = {}
        for oi in order.items.select_related('food_item'):
            for r in StoreRecipe.objects.filter(food_item=oi.food_item).select_related('ingredient'):
                need[r.ingredient] = need.get(r.ingredient, Decimal('0')) + r.quantity * oi.quantity

        short = [
            f'{g.name} (need {qty.quantize(Decimal("0.001"))}, have {g.current_stock} {g.unit})'
            for g, qty in need.items() if g.current_stock < qty
        ]
        if short and not force and getattr(settings, 'STORE_ENABLED', True):
            return {'ok': False, 'short': True, 'error': 'Not enough stock: ' + ', '.join(short)}, 409

        order.status = 'confirmed'
        order.save(update_fields=fields)
    return {'ok': True, 'status': 'confirmed'}, 200


def _razorpay_refund(order, reason=''):
    """Full refund to the customer's original payment method. Returns (ok, error).
    Never raises: a failed refund must not break the reject action."""
    payment_id = getattr(order, 'razorpay_payment_id', '')
    if not payment_id:
        return False, 'No Razorpay payment id saved for this order.'
    if payment_id.startswith('mock_'):
        # Mock checkout: no real money moved, so there is nothing to send to Razorpay.
        return True, ''
    if razorpay is None:
        return False, 'razorpay library is not installed.'
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '')
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '')
    if not key_id or not key_secret:
        return False, 'Razorpay keys are missing in settings.'
    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        client.payment.refund(payment_id, {
            'amount': int(order.total_amount * 100),   # rupees -> paise
            'notes': {'order_id': str(order.id), 'reason': (reason or 'Order rejected')[:200]},
        })
        return True, ''
    except Exception as e:
        logger.exception('Razorpay refund failed for order %s', order.id)
        return False, str(e)[:200]


# The reject logic lives in a helper so the SERVER can use it too.
def _reject_pending_order(pk, reason, user):
    """pending -> cancelled. Gives the coupon back, logs it, emails the customer.
    Returns (payload_dict, http_status)."""
    reason = (reason or '').strip()[:200]

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
        if order.status != 'pending':
            return {'ok': False, 'error': f'Order #{order.id} is already {order.status}.'}, 409

        order.status = 'cancelled'
        order.save(update_fields=['status'])

        # Give the coupon use back so usage-limited coupons are not wasted
        if order.coupon_id:
            Coupon.objects.filter(pk=order.coupon_id, times_used__gt=0).update(times_used=F('times_used') - 1)

        # Log it (is_read=True so the admin badge doesn't count it).
        # The customer's My Orders page reads the reason from this message.
        note = f'Order #{order.id} rejected' + (f': {reason}' if reason else '')
        Notification.objects.create(
            kind='rejected', order=order, created_by=user, is_read=True, message=note[:255])

    # Needs a refund if the customer already paid online
    refund = order.payment_method == 'online' and order.payment_status == 'paid'

    # Automatic Razorpay refund (outside the transaction, so a slow API call holds no DB lock).
    # If it fails, the order stays rejected and shows as "Refund pending" for a manual refund.
    refund_done, refund_error = False, ''
    if refund and not order.refunded:
        refund_done, refund_error = _razorpay_refund(order, reason)
        if refund_done:
            order.refunded = True
            order.save(update_fields=['refunded'])

    # Tell the customer (a mail failure must not break the reject action)
    if order.user.email:
        try:
            # Suggest available dishes from the same category(ies) as the rejected order
            ordered = [oi.food_item for oi in order.items.select_related('food_item')]
            alt = FoodItem.objects.filter(is_available=True).exclude(pk__in=[f.pk for f in ordered])
            same_cat = alt.filter(category_id__in={f.category_id for f in ordered})
            suggestions = list(same_cat[:3] or alt[:3])
            suggest_text = ""
            if suggestions:
                suggest_text = "\nHow about trying one of these instead?\n" + "\n".join(
                    f"- {f.name} (Rs.{f.price})" for f in suggestions) + "\n"

            body = (
                f"Hi {order.user.get_full_name() or order.user.username},\n\n"
                f"Sorry, we could not accept your order #{order.id}.\n"
                + (f"Reason: {reason}\n" if reason else "")
                + ("\nYour online payment has been refunded. It reaches your account in 5-7 working days.\n"
                   if refund_done else
                   "\nYour online payment will be refunded to you.\n" if refund else "")
                + suggest_text
                + "\nPlease place a new order or visit us again. We are sorry for the trouble.\n\n"
                "Chandru Restaurant"
            )
            send_mail(
                subject=f'Order #{order.id} - Could not be accepted',
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[order.user.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception('Could not send reject email for order %s', order.id)

    return {'ok': True, 'status': 'cancelled', 'refund': refund,
            'refunded': refund_done, 'refund_error': refund_error}, 200


@admin_required
@require_POST
def approve_order(request, pk):
    """Admin approves a paid/pending order -> it appears on the Kitchen board.
    (Admin override. The normal flow is now: Server -> 'Send to Kitchen'.)"""
    payload, code = _approve_pending_order(
        pk,
        force=request.POST.get('force') == '1',
        table_id=(request.POST.get('table') or '').strip() or None,
    )
    return JsonResponse(payload, status=code)


@admin_required
@require_POST
def reject_order(request, pk):
    """Admin rejects a pending order -> cancelled, never reaches the kitchen."""
    payload, code = _reject_pending_order(pk, request.POST.get('reason'), request.user)
    return JsonResponse(payload, status=code)


@admin_required
def order_stock_check(request, pk):
    """Popup data for 'Check Stock' on Manage Orders (look before approving)."""
    from store.models import GroceryItem as StoreGrocery, RecipeIngredient as StoreRecipe

    order = get_object_or_404(
        Order.objects.prefetch_related('items__food_item'), pk=pk)

    # Grocery this order needs, from each food item's store recipe
    need = {}
    no_recipe = []
    for oi in order.items.all():
        recipe = list(StoreRecipe.objects.filter(food_item=oi.food_item).select_related('ingredient'))
        if not recipe:
            no_recipe.append(oi.food_item.name)
        for r in recipe:
            need[r.ingredient] = need.get(r.ingredient, Decimal('0')) + r.quantity * oi.quantity

    requirements = [{
        'name': g.name,
        'unit': g.unit,
        'need': float(qty),
        'have': float(g.current_stock),
        'short': g.current_stock < qty,
    } for g, qty in need.items()]
    requirements.sort(key=lambda r: (not r['short'], r['name']))
    # Today's demand per food item (all non-cancelled orders placed today, incl. this one)
    todays = (
        OrderItem.objects
        .filter(order__created_at__date=timezone.localdate())
        .exclude(order__status='cancelled')
        .values('food_item__name')
        .annotate(qty=Sum('quantity'), orders=Count('order', distinct=True))
        .order_by('-qty')
    )
    today_map = {t['food_item__name']: t for t in todays}

    items = [{
        'name': oi.food_item.name,
        'qty': oi.quantity,
        'available': getattr(oi.food_item, 'is_available', True),
        'today_qty': today_map.get(oi.food_item.name, {}).get('qty', 0),
        'today_orders': today_map.get(oi.food_item.name, {}).get('orders', 0),
    } for oi in order.items.all()]

    today_demand = [{
        'name': t['food_item__name'],
        'qty': t['qty'],
        'orders': t['orders'],
    } for t in todays]

    store_items = StoreGrocery.objects.all()
    if any(f.name == 'is_active' for f in StoreGrocery._meta.get_fields()):
        store_items = store_items.filter(is_active=True)
    groceries = [{
        'name': g.name,
        'qty': float(g.current_stock),
        'unit': g.unit,
        'low': bool(g.is_low or g.is_out),
    } for g in store_items.order_by('name')]

    return JsonResponse({
        'order_id': order.id,
        'items': items,
        'today_demand': today_demand,
        'requirements': requirements,
        'no_recipe': no_recipe,
        'groceries': groceries,
    })


def _stock_base(user):
    """Store Manager sees the store sidebar; chef sees the kitchen sidebar."""
    from .decorators import _is_store_manager
    return 'store/store_base.html' if _is_store_manager(user) else 'dashboard/base_admin.html'


# ---- Grocery ----
@store_required
def manage_grocery(request):
    """List grocery stock and add new items."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        unit = request.POST.get('unit', 'kg')
        valid_units = {value for value, _ in GroceryItem.UNIT_CHOICES}

        if not name:
            messages.error(request, 'Item name is required.')
            return redirect('dashboard:manage_grocery')
        if unit not in valid_units:
            unit = 'kg'

        try:
            quantity = Decimal(request.POST.get('quantity') or '0')
            limit = Decimal(request.POST.get('low_stock_limit') or '5')
            if quantity < 0 or limit < 0:
                raise ValueError('negative')
        except Exception:
            messages.error(request, 'Enter valid, non-negative numbers.')
            return redirect('dashboard:manage_grocery')

        try:
            GroceryItem.objects.create(name=name, quantity=quantity, unit=unit, low_stock_limit=limit)
            messages.success(request, f'{name} added.')
        except Exception as e:
            logger.exception('Could not add grocery item')
            messages.error(request, f'Could not save: {e}')
        return redirect('dashboard:manage_grocery')

    from .models import StockRequest
    return render(request, 'dashboard/manage_grocery.html', {
        'items': GroceryItem.objects.all(),
        'units': GroceryItem.UNIT_CHOICES,
        'pending_requests': StockRequest.objects.filter(status='pending')
                            .select_related('item', 'requested_by').order_by('-id'),
    })


@store_required
@require_POST
def update_grocery(request, pk):
    from .models import StockRequest
    item = get_object_or_404(GroceryItem, pk=pk)
    try:
        quantity = Decimal(request.POST.get('quantity') or '0')
        if quantity < 0:
            raise ValueError('negative')
    except Exception:
        messages.error(request, 'Enter a valid number.')
        return redirect('dashboard:manage_grocery')

    old = item.quantity
    item.quantity = quantity
    item.save()

    if quantity != old:
        if quantity > old:
            msg = f'📦 Store restocked {item.name}: {old} → {quantity} {item.unit}'
        else:
            msg = f'📋 Store updated {item.name}: {old} → {quantity} {item.unit}'

        # Kitchen's pending "out of stock" reports for this item are now answered
        if quantity > 0:
            StockRequest.objects.filter(item=item, status='pending').update(
                status='restocked',
                restocked_message=msg[:200],
                restocked_at=timezone.now(),
            )

        # Live message for the kitchen (is_read=True so the admin badge doesn't count it)
        Notification.objects.create(
            kind='stock_in', message=msg[:255], created_by=request.user, is_read=True)

    messages.success(request, f'{item.name} updated.')
    return redirect('dashboard:manage_grocery')


@store_required
@require_POST
def delete_grocery(request, pk):
    get_object_or_404(GroceryItem, pk=pk).delete()
    return redirect('dashboard:manage_grocery')


# ---- Reports ----
@admin_required
def sales_reports(request):
    ranges = [
        ('today', 'Today'),
        ('week', 'This Week'),
        ('month', 'This Month'),
        ('year', 'This Year'),
        ('all', 'All Time'),
    ]
    labels = dict(ranges)

    rng = request.GET.get('range', 'today')
    if rng not in labels:
        rng = 'today'

    today = timezone.localdate()
    if rng == 'today':
        start = today
    elif rng == 'week':
        start = today - timedelta(days=today.weekday())  # Monday
    elif rng == 'month':
        start = today.replace(day=1)
    elif rng == 'year':
        start = today.replace(month=1, day=1)
    else:
        start = None

    orders = Order.objects.filter(status__in=PAID_STATUSES)
    if start:
        orders = orders.filter(created_at__date__gte=start)

    # CSV export: all orders in the selected range
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="sales_%s.csv"' % rng
        writer = csv.writer(response)
        writer.writerow(['Order ID', 'Customer', 'Amount', 'Payment', 'Payment Status', 'Status', 'Date'])
        for o in orders.select_related('user').order_by('-created_at'):
            writer.writerow([
                o.id,
                o.user.get_full_name() or o.user.username,
                o.total_amount,
                o.payment_method,
                o.payment_status,
                o.status,
                timezone.localtime(o.created_at).strftime('%d %b %Y %H:%M'),
            ])
        return response

    def total(qs):
        return qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    total_orders = orders.count()
    total_revenue = total(orders)
    avg_order_value = round(total_revenue / total_orders) if total_orders > 0 else 0

    online_orders = orders.filter(payment_method='online')
    cash_orders = orders.filter(payment_method='cash')
    cash_collected_orders = cash_orders.exclude(payment_status='cash_pending')
    cash_pending_orders = cash_orders.filter(payment_status='cash_pending')

    # CHANGED: the order table was [:50]. Now it is paginated (20 per page),
    # so older orders in the selected range can be seen too. Totals above still use ALL orders.
    page_obj, querystring = _paginate(
        request, orders.select_related('user').order_by('-created_at', '-id'), per_page=20)

    context = {
        'range': rng,
        'range_label': labels[rng],
        'ranges': ranges,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'recent_orders': page_obj,     # loop over this exactly like before
        'page_obj': page_obj,
        'querystring': querystring,
        'online_revenue': total(online_orders),
        'online_count': online_orders.count(),
        'cash_revenue': total(cash_collected_orders),
        'cash_count': cash_collected_orders.count(),
        'cash_pending_amount': total(cash_pending_orders),
        'cash_pending_count': cash_pending_orders.count(),
        'chart_data': _sales_chart_data(start),   # NEW: charts
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
@require_POST
def delete_table(request, pk):
    """POST only. A table that is still held by an approved booking (not opened yet),
    one that a pending booking of today / a later day has chosen, or one held by a running
    order, is not deleted. Past, rejected and opened bookings do not block it
    (the booking history stays, only its table link becomes empty)."""
    table = get_object_or_404(Table, pk=pk)

    # CHANGED: no 2-hour window. An approved booking holds the table until the admin opens it.
    today = timezone.localdate()
    live_bookings = (TableBooking.objects
                     .filter(Q(table=table) | Q(extra_tables=table),
                             status__in=['pending', 'approved'],
                             released=False)
                     .distinct())
    has_live_booking = False
    for b in live_bookings:
        if b.status == 'approved' or b.booking_date >= today:
            has_live_booking = True
            break
    if has_live_booking:
        messages.error(
            request,
            f'Table {table.number} has a booking that is not finished, so it cannot be deleted. '
            'Open the table first.'
        )
        return redirect('dashboard:manage_tables')

    if _order_holding_table(table.id):
        messages.error(
            request,
            f'Table {table.number} is held by a running order, so it cannot be deleted.'
        )
        return redirect('dashboard:manage_tables')

    number = table.number
    try:
        table.delete()
    except ProtectedError:
        messages.error(
            request,
            f'Table {number} is used by old orders, so it cannot be deleted.'
        )
        return redirect('dashboard:manage_tables')
    messages.success(request, f'Table {number} deleted.')
    return redirect('dashboard:manage_tables')


# ---- QR Codes ----
@admin_required
def table_qr_codes(request):
    return render(request, 'dashboard/table_qr_codes.html', {
        'tables': Table.objects.all().order_by('number')
    })


# ---- Kitchen ----
def _kitchen_order(qs):
    """Kitchen board order: ASAP orders (no arrival_time) always come first.
    ASAP orders: oldest first, so the one waiting longest is on top.
    Scheduled orders (customer gave an arrival time): soonest arrival first.
    NEW: prepared_by is loaded together, so a card can show the chef name without extra queries."""
    return qs.select_related('prepared_by').annotate(
        asap_rank=Case(
            When(arrival_time__isnull=True, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    ).order_by('asap_rank', 'arrival_time', 'created_at', 'id')


@kitchen_required
def kitchen_dashboard(request):
    confirmed_orders = _kitchen_order(Order.objects.filter(status='confirmed'))
    preparing_orders = _kitchen_order(Order.objects.filter(status='preparing'))
    ready_orders = _kitchen_order(Order.objects.filter(status='ready'))

    grocery_items, store_low = _store_stock_overview()
    low_stock_count = len(store_low)

    context = {
        'confirmed_orders': confirmed_orders,
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
        'grocery_items': grocery_items,
        'low_stock_count': low_stock_count,
    }
    return render(request, 'dashboard/kitchen.html', context)


@store_or_kitchen_required
def kitchen_stock(request):
    """Kitchen 'Out of Stock' page: report an item that ran out and see restock updates."""
    from .models import StockRequest
    items = GroceryItem.objects.all().order_by('name')
    pending_ids = set(StockRequest.objects.filter(status='pending').values_list('item_id', flat=True))
    recent = (StockRequest.objects.filter(status='restocked')
              .exclude(restocked_message='')
              .select_related('item')
              .order_by('-restocked_at')[:5])
    return render(request, 'dashboard/kitchen_out_of_stock.html', {
        'grocery_items': items,
        'pending_ids': pending_ids,
        'recent_restocks': recent,
        'base_tpl': _stock_base(request.user),
    })


@store_or_kitchen_required
def kitchen_stock_update(request):
    """Kitchen 'Stock Update' page: add new stock or set the exact count."""
    items = GroceryItem.objects.all().order_by('name')
    return render(request, 'dashboard/kitchen_stock_view.html', {
        'grocery_items': items,
        'base_tpl': _stock_base(request.user),
    })


# ---- Kitchen "Out of Stock" reasons (NEW) ----
# reason code (sent by kitchen_stock.html chips) -> text used in the alert message.
# Each option gives its own message, so the sound / voice can be different per option.
STOCK_OUT_REASONS = {
    'out_of_stock': 'is out of stock',
    'finished':     'is finished',
    'expired':      'is expired',
    'spoiled':      'is spoiled',
    'urgent':       'is needed URGENTLY',
    'quality':      'has a quality issue',
}


def _stock_out_reason_from_message(message):
    """Finds which reason an alert message was created with, so the poll can
    send it to the sound script (no database change needed)."""
    for code, phrase in STOCK_OUT_REASONS.items():
        if f' {phrase} (' in (message or ''):
            return code
    return 'out_of_stock'


@store_or_kitchen_required
@require_POST
def kitchen_request_stock(request, pk):
    """Chef reports an item (out of stock / expired / spoiled / urgent ...) -> Store Manager gets an alert."""
    from .models import StockRequest
    item = get_object_or_404(GroceryItem, pk=pk)
    if StockRequest.objects.filter(item=item, status='pending').exists():
        return JsonResponse({'ok': False, 'error': 'Already reported. Waiting for store to restock.'}, status=400)

    note = (request.POST.get('note') or '').strip()[:120]
    reason = (request.POST.get('reason') or 'out_of_stock').strip()     # CHANGED: reason from the chip
    if reason not in STOCK_OUT_REASONS:
        reason = 'out_of_stock'

    StockRequest.objects.create(item=item, note=note, requested_by=request.user)
    msg = f'🚫 Kitchen → Store: {item.name} {STOCK_OUT_REASONS[reason]} ({item.quantity} {item.unit} left). Please restock.'
    if note:
        msg += f' Note: {note}'
    Notification.objects.create(kind='stock_out', message=msg[:255], created_by=request.user)

    return JsonResponse({'ok': True, 'name': item.name, 'reason': reason})


@store_or_kitchen_required
def kitchen_notifications_poll(request):
    """Latest store stock update for the kitchen toast; also latest kitchen report for the store toast."""
    from .models import StockRequest
    latest = (StockRequest.objects.filter(status='restocked')
              .exclude(restocked_message='')
              .order_by('-restocked_at')
              .first())
    stock_in = Notification.objects.filter(kind='stock_in').order_by('-id').first()
    stock_out = Notification.objects.filter(kind='stock_out').order_by('-id').first()
    return JsonResponse({
        'latest_ts': int(latest.restocked_at.timestamp() * 1000) if latest and latest.restocked_at else 0,
        'latest_message': latest.restocked_message if latest else '',
        'pending': StockRequest.objects.filter(status='pending').count(),
        'stock_in_id': stock_in.id if stock_in else 0,
        'stock_in_message': stock_in.message if stock_in else '',
        'stock_out_id': stock_out.id if stock_out else 0,
        'stock_out_message': stock_out.message if stock_out else '',
        'stock_out_reason': _stock_out_reason_from_message(stock_out.message) if stock_out else '',  # NEW
    })


@store_or_kitchen_required
@require_POST
def kitchen_update_grocery(request, pk):
    """Kitchen staff can only update the stock quantity (no add / delete).
    mode=set -> exact remaining count, mode=add -> new stock arrived (adds to current)."""
    # Kitchen stock editing is switched off on purpose: only the store updates stock.
    # The code below is kept so it can be switched on again by removing this return.
    return JsonResponse({'ok': False, 'error': 'Stock is updated by the store only.'}, status=403)
    item = get_object_or_404(GroceryItem, pk=pk)
    mode = request.POST.get('mode', 'set')
    try:
        value = Decimal(request.POST.get('quantity') or '0')
        if value < 0:
            raise ValueError('negative')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Enter a valid quantity.'}, status=400)

    old = item.quantity
    item.quantity = old + value if mode == 'add' else value
    item._by_kitchen = True  # kitchen changes must not trigger the admin-restock notification
    item.save()

    low = item.is_low
    if mode == 'add':
        msg = f'📦 Kitchen received new stock: {item.name} +{value} {item.unit} (now {item.quantity} {item.unit})'
    else:
        msg = f'{"⚠️" if low else "✅"} Kitchen stock: {item.name} {old} → {item.quantity} {item.unit}' + (' (LOW)' if low else '')
    Notification.objects.create(kind='stock', message=msg[:255], created_by=request.user)

    return JsonResponse({'ok': True, 'quantity': str(item.quantity), 'added': str(value), 'low': low})


# ---- Order status flow (server -> kitchen -> server) ----
#
#   pending -> confirmed                         (server: "Send to Kitchen", see server_send_to_kitchen)
#   confirmed -> preparing -> ready              (kitchen)
#   ready -> delivered                           (server)
#
# Which status moves each role is allowed to make. Admin can make any move.
ROLE_TRANSITIONS = {
    'kitchen': {('confirmed', 'preparing'), ('preparing', 'ready')},
    'server': {('ready', 'delivered')},
}


def _staff_role(user):
    if user.is_superuser:
        return 'admin'
    profile = getattr(user, 'profile', None)
    return profile.role if profile else None


def _role_required(*allowed_roles):
    """Only logged-in staff whose role is in allowed_roles can open the view.
    Not logged in -> login page. Wrong role -> 403."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated or not request.user.is_active:
                return redirect_to_login(request.get_full_path())
            if _staff_role(request.user) not in allowed_roles:
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def _board_for(role, new_status):
    if role == 'server':
        return 'dashboard:server_dashboard'
    if role == 'admin' and new_status == 'delivered':
        return 'dashboard:server_dashboard'
    return 'dashboard:kitchen_dashboard'


def _transition_error(order, user, role, new_status):
    """Returns a message if this staff member may not move the order to new_status, else None."""
    if role != 'admin':
        if (order.status, new_status) not in ROLE_TRANSITIONS.get(role, set()):
            if new_status == 'delivered':
                return 'Only the server can mark an order as served.'
            return f'Order #{order.id} cannot be moved to "{new_status}" from "{order.status}".'
    return None


def _email_customer(order, subject, message):
    """Email the customer. A mail failure must never break the staff action."""
    if not order.user.email:
        return
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[order.user.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception('Could not send "%s" email for order %s', subject, order.id)


# Smallest and largest ready-time promise the chef can make (minutes).
ETA_MIN_MINUTES = 5
ETA_MAX_MINUTES = 90


def _parse_eta_minutes(raw):
    """'20' -> 20. Missing, bad or out-of-range input gives None (no estimate is saved)."""
    try:
        minutes = int((raw or '').strip())
    except (ValueError, AttributeError):
        return None
    return minutes if ETA_MIN_MINUTES <= minutes <= ETA_MAX_MINUTES else None


@_role_required('admin', 'kitchen', 'server')
@require_POST
def update_order_status(request, pk, new_status):
    role = _staff_role(request.user)

    valid_statuses = {value for value, _ in Order.STATUS_CHOICES}
    if new_status not in valid_statuses:
        messages.error(request, 'Invalid order status.')
        return redirect(_board_for(role, new_status))

    # Lock the row so two people (or a double click) cannot move the same order twice
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
        error = _transition_error(order, request.user, role, new_status)
        if not error:
            fields = ['status']
            order.status = new_status
            if new_status == 'preparing':
                # Chef picks "ready in N minutes"; the customer sees the time left.
                minutes = _parse_eta_minutes(request.POST.get('eta_minutes'))
                if minutes:
                    order.ready_by = timezone.now() + timedelta(minutes=minutes)
                    fields.append('ready_by')

                # NEW: remember which chef took this order.
                # Only a kitchen login is saved (an admin override leaves it as it was).
                if role == 'kitchen':
                    order.prepared_by = request.user
                    fields.append('prepared_by')
            order.save(update_fields=fields)

    if error:
        messages.error(request, error)
        return redirect(_board_for(role, new_status))

    name = order.user.get_full_name() or order.user.username

    if new_status == 'delivered':
        if order.table:
            where = f'Table {order.table.number}'
        else:
            where = 'takeaway counter'

        Notification.objects.create(
            kind='served',
            order=order,
            created_by=request.user,
            message=f'Order #{order.id} served to {where} by {request.user.username}'[:255],
        )
        _email_customer(
            order,
            f'Order #{order.id} - Delivered',
            f"Hi {name},\n\n"
            f"Your order has been delivered. Enjoy your meal! 🍽️\n\n"
            f"Order #{order.id}\n"
            f"Status: Delivered\n\n"
            f"Thank you for choosing Chandru Restaurant!",
        )

    return redirect(_board_for(role, new_status))


def _deduct_stock_for_order(order):
    """Use up grocery stock for an order using the store recipes.
    Used when an admin moves an order that never went through Approve (pending or rejected)
    straight to an active status, and for server orders. Safe to call more than once:
    the store service skips orders that were already deducted."""
    from store.services import deduct_stock_for_order
    deduct_stock_for_order(order)


@admin_required
@require_POST
def api_order_status(request, pk):
    """Used by the status dropdown on Manage Orders (admin only)."""
    try:
        status = json.loads(request.body).get('status')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Invalid data'}, status=400)

    if status not in {value for value, _ in Order.STATUS_CHOICES}:
        return JsonResponse({'ok': False, 'error': 'Invalid status'}, status=400)

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=pk)

        was_cancelled = order.status == 'cancelled'
        now_cancelled = status == 'cancelled'

        # An order whose money was already paid back must not be re-opened (the customer
        # would be served without paying). They should place a new order instead.
        if was_cancelled and not now_cancelled and order.refunded:
            return JsonResponse(
                {'ok': False, 'error': f'Order #{order.id} was already refunded, so it cannot be re-opened.'},
                status=409)

        # Pending/rejected orders never had their stock used. If the admin activates one
        # from the dropdown, use the stock now so the numbers stay correct.
        needs_stock = order.status in ('pending', 'cancelled') and status in PAID_STATUSES

        if needs_stock:
            _deduct_stock_for_order(order)

        # Keep the coupon count right: cancelling gives the use back (same as Reject),
        # re-opening a cancelled order counts it as used again.
        if order.coupon_id:
            if now_cancelled and not was_cancelled:
                Coupon.objects.filter(pk=order.coupon_id, times_used__gt=0).update(times_used=F('times_used') - 1)
            elif was_cancelled and not now_cancelled:
                Coupon.objects.filter(pk=order.coupon_id).update(times_used=F('times_used') + 1)

        order.status = status
        order.save(update_fields=['status'])
    return JsonResponse({'ok': True, 'status': status, 'stock_used': needs_stock and getattr(settings, 'AUTO_DEDUCT_STOCK', True)})


# ---- Server ----
def _orderable_food_items():
    """Menu items the server can add to an order (hides unavailable ones if the model has that flag)."""
    qs = FoodItem.objects.all()
    if any(f.name == 'is_available' for f in FoodItem._meta.get_fields()):
        qs = qs.filter(is_available=True)
    return qs.order_by('name')


def _tables_info():
    """Table chips for the Take Order popup: free / booked now / occupied by an order."""
    held = {
        o.table_id: o.id
        for o in Order.objects.filter(table__isnull=False, table_released=False)
                              .exclude(status='cancelled')
    }
    return [{
        'id': t.id,
        'number': t.number,
        'busy': is_table_busy_now(t.id),
        'held_by': held.get(t.id),
    } for t in Table.objects.all().order_by('number')]


# ---- NEW: combined bill per table ----
# When the same table orders again while the customer is still seated (Order #13, then #14),
# the kitchen still gets a separate ticket for each order, but the BILL is ONE per table.
def _table_bill_orders(order):
    """All orders of the same sitting: same table, customer not left yet, not cancelled.
    Takeaway / already-released / cancelled orders get their own single bill."""
    if order.table_id and not order.table_released and order.status != 'cancelled':
        return list(Order.objects.filter(table_id=order.table_id, table_released=False)
                    .exclude(status='cancelled')
                    .select_related('user', 'table')
                    .prefetch_related('items__food_item')
                    .order_by('created_at', 'id'))
    return [order]


@server_required
def server_dashboard(request):
    # Customer orders waiting for the server. Server sends them to the kitchen (or rejects).
    # Online orders whose payment is not finished yet are not shown.
    # CHANGED: newest order comes first (-created_at).
    new_orders = (Order.objects.filter(status='pending')
                  .exclude(payment_status='pending')
                  .select_related('table', 'user')
                  .prefetch_related('items__food_item')
                  .order_by('-created_at'))

    ready_orders = (Order.objects.filter(status='ready')
                    .select_related('table', 'user').order_by('created_at'))
    delivered_today = (Order.objects.filter(status='delivered', created_at__date=timezone.localdate())
                       .order_by('-created_at'))

    # Served, but the customer has not left yet (table still held until "Customer Left")
    dining_orders = list(Order.objects.filter(status='delivered', table__isnull=False, table_released=False)
                         .select_related('table', 'user').order_by('created_at'))

    # NEW: total / amount due for the WHOLE table (all its running orders), so the Dining Now
    # card can show one combined amount. In the template use:
    #   {{ o.table_total }}  {{ o.table_due }}  {{ o.table_order_count }}
    # The bill link is: {% url 'dashboard:server_table_bill' o.id %}
    _table_totals = {}
    for tid, amount, paid in (Order.objects
                              .filter(table_id__in=[o.table_id for o in dining_orders], table_released=False)
                              .exclude(status='cancelled')
                              .values_list('table_id', 'total_amount', 'payment_status')):
        t = _table_totals.setdefault(tid, {'total': Decimal('0'), 'due': Decimal('0'), 'count': 0})
        t['total'] += amount
        t['count'] += 1
        if paid != 'paid':
            t['due'] += amount
    for o in dining_orders:
        t = _table_totals.get(o.table_id, {'total': o.total_amount, 'due': Decimal('0'), 'count': 1})
        o.table_total = t['total']
        o.table_due = t['due']
        o.table_order_count = t['count']

    # NEW: show ONE Dining Now card per table (the first order of that table carries the
    # combined total), so Table 10 with orders #13 and #14 is a single card
    _seen_tables = set()
    _one_per_table = []
    for o in dining_orders:
        if o.table_id in _seen_tables:
            continue
        _seen_tables.add(o.table_id)
        _one_per_table.append(o)
    dining_orders = _one_per_table

    # CHANGED: only APPROVED bookings are shown to the server. A pending or rejected booking
    # has no table chosen, so it showed as "No table assigned" and confused the server.
    # Extra tables are loaded too: in the template use {{ b.table_numbers }} and
    # {{ b.total_seats }} (they read booking.all_tables, the same data the customer sees).
    todays_bookings = list(TableBooking.objects
                          .filter(booking_date=timezone.localdate(), status='approved')
                          .select_related('table')
                          .prefetch_related('extra_tables')
                          .order_by('booking_time'))

    # NEW: tell the server which booking is finished and which one is on now.
    #   completed = the admin clicked "Open Table" (the guests have left)
    #   active    = booked time has started and the table is still held
    #   upcoming  = booked time has not come yet
    # Active first, then upcoming, finished ones last (each group by time).
    now = timezone.now()
    for b in todays_bookings:
        if b.released:
            b.ui_state = 'completed'
        elif _booking_start(b) <= now:
            b.ui_state = 'active'
        else:
            b.ui_state = 'upcoming'
    _state_rank = {'active': 0, 'upcoming': 1, 'completed': 2}
    todays_bookings.sort(key=lambda b: (_state_rank[b.ui_state], b.booking_time))

    context = {
        'new_orders': new_orders,
        'ready_orders': ready_orders,
        'delivered_today': delivered_today,
        'dining_orders': dining_orders,
        'todays_bookings': todays_bookings,
        'tables': _free_tables(),
        'tables_info': _tables_info(),
        'food_items': _orderable_food_items(),
    }
    return render(request, 'dashboard/server.html', context)


# NEW: ONE bill page for a whole table (any order id of that table opens the same bill)
@server_required
def server_table_bill(request, pk):
    """pk = any order id of the table. Shows ONE bill with all orders of that table."""
    order = get_object_or_404(Order.objects.select_related('table'), pk=pk)
    orders = _table_bill_orders(order)

    # Same dish ordered twice (e.g. Apple in order 13 and 14) becomes one line
    lines = {}
    for o in orders:
        for oi in o.items.all():
            unit = getattr(oi, 'price', None) or oi.food_item.price
            key = (oi.food_item_id, unit)
            if key not in lines:
                lines[key] = {'name': oi.food_item.name, 'qty': 0, 'unit': unit, 'amount': Decimal('0')}
            lines[key]['qty'] += oi.quantity
            lines[key]['amount'] += unit * oi.quantity

    items_total = sum((l['amount'] for l in lines.values()), Decimal('0'))
    grand_total = sum((o.total_amount for o in orders), Decimal('0'))
    paid_total = sum((o.total_amount for o in orders if o.payment_status == 'paid'), Decimal('0'))
    due_total = grand_total - paid_total
    can_collect = any(o.payment_method == 'cash' and o.payment_status != 'paid' for o in orders)

    return render(request, 'dashboard/table_bill.html', {
        'order': order,
        'orders': orders,
        'table': order.table,
        'lines': list(lines.values()),
        'items_total': items_total,
        'adjustment': grand_total - items_total,   # coupon / discount difference, if any
        'grand_total': grand_total,
        'paid_total': paid_total,
        'due_total': due_total,
        'can_collect': can_collect,
        'now': timezone.now(),
    })


# NEW: one click marks every unpaid cash order of the table as paid
@server_required
@require_POST
def server_table_cash_received(request, pk):
    """One click: every unpaid cash order of this table is marked paid."""
    order = get_object_or_404(Order, pk=pk)
    if order.table_id and not order.table_released and order.status != 'cancelled':
        qs = Order.objects.filter(table_id=order.table_id, table_released=False)
    else:
        qs = Order.objects.filter(pk=order.pk)
    (qs.filter(payment_method='cash')
       .exclude(status='cancelled')
       .exclude(payment_status='paid')
       .update(payment_status='paid'))
    return redirect('dashboard:server_table_bill', pk=order.pk)


# Server -> Kitchen
@server_required
@require_POST
def server_send_to_kitchen(request, pk):
    """Server checks the customer's order and sends it to the Kitchen board.
    The table normally comes from the customer's QR scan (no table picking needed).
    POST: force=1 to send even if stock is short."""
    payload, code = _approve_pending_order(
        pk,
        force=request.POST.get('force') == '1',
        table_id=(request.POST.get('table') or '').strip() or None,
    )
    return JsonResponse(payload, status=code)


# Server rejects a customer order
@server_required
@require_POST
def server_reject_order(request, pk):
    payload, code = _reject_pending_order(pk, request.POST.get('reason'), request.user)
    return JsonResponse(payload, status=code)


# Always returns JSON (the server.html "Customer Left" button reads it)
@server_required
@require_POST
def server_free_table(request, pk):
    """Server taps 'Customer Left' (pk = order id). Frees the table for ALL orders on it.
    CHANGED: refused while any order on this table is unpaid or not served yet,
    so nobody can leave without paying and a new order (extra juice...) is never lost."""
    order = get_object_or_404(Order, pk=pk)

    if not order.table_id:
        msg, ok = 'This order has no table.', False
    elif order.table_released:
        msg, ok = f'Table #{order.table.number} is already free.', False
    else:
        on_table = list(Order.objects.filter(table_id=order.table_id, table_released=False)
                        .exclude(status='cancelled'))
        unpaid = [o for o in on_table if o.payment_status != 'paid']
        unserved = [o for o in on_table if o.status != 'delivered']
        if unpaid:
            o = unpaid[0]
            msg, ok = f'Payment pending for order #{o.id}. Collect Rs.{o.total_amount} first.', False
        elif unserved:
            msg, ok = f'Order #{unserved[0].id} is not served yet.', False
        else:
            Order.objects.filter(pk__in=[o.id for o in on_table]).update(table_released=True)
            msg, ok = f'Table #{order.table.number} is free now.', True

    payload = {'ok': ok, 'message': msg, 'table': order.table.number if order.table_id else None}
    if not ok:
        payload['error'] = msg
    return JsonResponse(payload, status=200 if ok else 400)


@server_required
@require_POST
def server_mark_cash_received(request, pk):
    """NEW: server collects the cash at the counter and marks the order paid.
    Only cash orders. Cancelled orders are refused.
    The order status is NOT changed here (the server serves it separately)."""
    order = get_object_or_404(Order, pk=pk, payment_method='cash')

    if order.status == 'cancelled':
        return JsonResponse(
            {'ok': False, 'success': False, 'error': f'Order #{order.id} is cancelled, so there is no cash to collect.'},
            status=400)

    if order.payment_status != 'paid':
        order.payment_status = 'paid'
        order.save(update_fields=['payment_status'])
    return JsonResponse({'ok': True, 'success': True, 'order_id': order.id})


@server_required
@require_POST
def server_take_order(request):
    """Server takes an order at the table. It lands straight on the Kitchen board (status 'confirmed'),
    paid in cash at the counter (cash_pending until the cash is marked received).
    A table whose customer is still eating can take another order (extra juice, dessert...)."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid request'}, status=400)

    rows = data.get('items') or []
    if not rows:
        return JsonResponse({'ok': False, 'error': 'Add at least one item'}, status=400)

    table = None
    table_id = data.get('table_id')
    if table_id is not None:
        table = Table.objects.filter(pk=table_id).first()
        if not table:
            return JsonResponse({'ok': False, 'error': 'Table not found'}, status=400)
        # CHANGED: a table already held by a customer who is still eating can take more orders.
        # Only a booking hold blocks the table.
        if not _order_holding_table(table.id) and is_table_busy_now(table.id):
            return JsonResponse({'ok': False, 'error': f'Table {table.number} is booked right now.'}, status=409)

    # Collect lines first so a bad line never leaves a half-made order behind
    lines = []
    total = Decimal('0')
    for row in rows:
        try:
            qty = int(row.get('quantity', 0))
            food_id = int(row.get('food_item_id'))
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'Invalid item'}, status=400)
        if qty < 1:
            continue
        food = FoodItem.objects.filter(pk=food_id).first()
        if not food:
            return JsonResponse({'ok': False, 'error': 'A menu item no longer exists'}, status=400)
        # A dish the admin has hidden (is_available=False) cannot be ordered, same as on the customer side
        if not food.is_available:
            return JsonResponse({'ok': False, 'error': f'{food.name} is not available right now.'}, status=400)
        lines.append((food, qty))
        total += food.price * qty   # price always comes from the DB, never the browser

    if not lines:
        return JsonResponse({'ok': False, 'error': 'Add at least one item'}, status=400)

    order_item_fields = {f.name for f in OrderItem._meta.get_fields()}

    with transaction.atomic():
        order = Order.objects.create(
            user=request.user,            # server's own account (no migration needed)
            table=table,                  # None => Takeaway
            order_type='dine_in' if table else 'takeaway',
            total_amount=total,
            status='confirmed',           # Kitchen board reads 'confirmed'
            payment_method='cash',
            payment_status='cash_pending',
            table_released=False,
        )
        for food, qty in lines:
            item_kwargs = {'order': order, 'food_item': food, 'quantity': qty}
            if 'price' in order_item_fields:
                item_kwargs['price'] = food.price
            OrderItem.objects.create(**item_kwargs)

        # Server orders skip Approve, so use up the grocery stock here
        _deduct_stock_for_order(order)

    return JsonResponse({'ok': True, 'order_id': order.id})


@server_required
def server_poll(request):
    """Polled by the server dashboard so new customer orders, finished orders,
    payment changes and cash-received alerts show up live."""
    # Customer orders waiting for the server. CHANGED: newest first + date/time sent to the page.
    new_orders = (Order.objects.filter(status='pending')
                  .exclude(payment_status='pending')
                  .select_related('table').order_by('-created_at'))
    orders = (Order.objects.filter(status='ready')
              .select_related('table').order_by('created_at'))

    # NEW: signature of everything the board shows (today's orders + orders still holding a table).
    # When the admin marks cash received, a table is freed, an order is served, etc., this value
    # changes, so the server page knows it must refresh itself without a manual reload.
    _board = (Order.objects
              .filter(Q(created_at__date=timezone.localdate())
                      | Q(table__isnull=False, table_released=False))
              .order_by('id')
              .values_list('id', 'status', 'payment_status', 'table_id', 'table_released', 'total_amount'))
    sig = hashlib.md5(repr(list(_board)).encode('utf-8')).hexdigest()

    # NEW: the latest "cash received" alert (created by the admin Mark Received button)
    last_cash = Notification.objects.filter(kind='cash_received').order_by('-id').first()

    return JsonResponse({
        'sig': sig,
        'cash_id': last_cash.id if last_cash else 0,
        'cash_message': last_cash.message if last_cash else '',
        'new_orders': [
            {
                'id': o.id,
                'table': o.table.number if o.table else None,
                'created_at': timezone.localtime(o.created_at).strftime('%d %b %Y, %I:%M %p'),
            }
            for o in new_orders
        ],
        'orders': [
            {'id': o.id, 'table': o.table.number if o.table else None}
            for o in orders
        ]
    })


# ---- NEW: Admin live-update poll (used by base_admin.html on EVERY admin page) ----
@admin_required
def admin_poll(request):
    """Polled by EVERY admin page (from base_admin.html). Returns one signature of all the data
    the admin pages show. When it changes, the open page refreshes itself."""
    today = timezone.localdate()

    paid_total = (Order.objects.filter(status__in=PAID_STATUSES)
                  .aggregate(s=Sum('total_amount'))['s'] or 0)

    # Orders: today's, still holding a table, or waiting for approval (any date)
    orders = list(
        Order.objects
        .filter(Q(created_at__date=today)
                | Q(table__isnull=False, table_released=False)
                | Q(status='pending'))
        .order_by('id')
        .values_list('id', 'status', 'payment_status', 'payment_method',
                     'table_id', 'table_released', 'refunded', 'total_amount'))

    # Bookings: pending ones + yesterday onwards
    bookings = list(
        TableBooking.objects
        .filter(Q(booking_date__gte=today - timedelta(days=1)) | Q(status='pending'))
        .order_by('id')
        .values_list('id', 'status', 'released', 'table_id', 'booking_date', 'booking_time'))

    users = User.objects.aggregate(
        n=Count('id'),
        active=Count('id', filter=Q(is_active=True)),
        staff=Count('id', filter=Q(is_staff=True)),
    )
    menu = list(FoodItem.objects.order_by('id').values_list('id', 'is_available', 'price'))
    tables = list(Table.objects.order_by('id').values_list('id', 'number', 'capacity'))

    latest_note = Notification.objects.first()
    _items, low = _store_stock_overview()

    parts = [
        Order.objects.count(), str(paid_total), orders,
        TableBooking.objects.count(), bookings,
        users, menu, tables,
        latest_note.id if latest_note else 0,
        Notification.objects.filter(is_read=False).count(),
        [(g.id, g.is_out) for g in low],
    ]
    sig = hashlib.md5(repr(parts).encode('utf-8')).hexdigest()
    return JsonResponse({'sig': sig})


# ---- Admin notifications (served orders) ----
@admin_required
def notifications_poll(request):
    latest = Notification.objects.first()
    return JsonResponse({
        'unread': Notification.objects.filter(is_read=False).count(),
        'latest_id': latest.id if latest else 0,
        'latest_message': latest.message if latest else '',
        'latest_kind': latest.kind if latest else '',
    })


@admin_required
@require_POST
def notifications_mark_read(request):
    Notification.objects.filter(is_read=False).update(is_read=True)
    return redirect('dashboard:admin_home')


@admin_required
@require_POST
def toggle_menu_availability(request, pk):
    """Admin switches a dish between Available / Not available."""
    item = get_object_or_404(FoodItem, pk=pk)
    item.is_available = not item.is_available
    item.save(update_fields=['is_available'])
    return JsonResponse({'ok': True, 'available': item.is_available})


# ---- Booking approval (Approve / Reject from Manage Bookings) ----
# These helpers read optional booking fields safely, so a different field name
# never crashes the page. Only 'status' is required on TableBooking.
def _first_attr(obj, names, default=''):
    """Value of the first attribute in `names` that exists on obj and is not empty."""
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ''):
            return value
    return default


def _booking_email(booking):
    user = getattr(booking, 'user', None)
    return (_first_attr(booking, ['email', 'customer_email'])
            or (getattr(user, 'email', '') if user else ''))


def _booking_name(booking):
    user = getattr(booking, 'user', None)
    fallback = ''
    if user:
        fallback = user.get_full_name() or user.username
    return _first_attr(booking, ['name', 'customer_name', 'full_name'], fallback) or 'Customer'


def _booking_details_text(booking):
    """Details for the customer email. CHANGED: a booking may have no table yet
    (rejected before the admin chose one) or several tables."""
    lines = []
    tables = _booking_tables(booking)
    if tables:
        lines.append(_tables_text(tables))
    lines += [
        f"Date: {booking.booking_date}",
        f"Time: {booking.booking_time}",
    ]
    members = _first_attr(booking, ['members', 'guests', 'party_size', 'num_guests', 'persons'], '')
    if members != '':
        lines.append(f"Members: {members}")
    return "\n".join(lines)


def _email_booking(booking, subject, body):
    """Email the customer. A mail failure must never break the admin action."""
    to = _booking_email(booking)
    if not to:
        return
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to],
            fail_silently=False,
        )
    except Exception:
        logger.exception('Could not send booking email for booking %s', booking.pk)


@admin_required
@require_POST
def approve_booking(request, pk):
    """pending -> approved. The admin chooses the table(s) here.

    POST fields:
      tables = one or more table ids (checkboxes / multi-select named 'tables')
      force  = '1' to approve even if a chosen table clashes with another booking/order

    Rules:
      - at least one table (a booking that already has tables from the old flow keeps them)
      - chosen seats must cover the guests (so one 7 seat table is enough for 7 guests)
      - CHANGED: a table that clashes is refused unless force=1. A clash is either
        "still held by an earlier booking/order" or "reserved for a LATER approved booking
        the same day" (see _slot_conflicts)
    The first table (lowest number) is the main table, the rest are extra tables."""
    force = request.POST.get('force') == '1'
    try:
        ids = sorted({int(i) for i in request.POST.getlist('tables')})
    except ValueError:
        messages.error(request, 'Invalid table selection.')
        return redirect('dashboard:manage_bookings')

    with transaction.atomic():
        # No select_related('table') here: the table may still be empty and row locks
        # cannot be taken on the nullable side of a join.
        booking = get_object_or_404(TableBooking.objects.select_for_update(), pk=pk)

        if booking.status != 'pending':
            messages.error(request, f'Booking #{booking.pk} is already {booking.status}.')
            return redirect('dashboard:manage_bookings')

        if ids:
            tables = list(Table.objects.filter(id__in=ids).order_by('number'))
            if len(tables) != len(ids):
                messages.error(request, 'One of the chosen tables no longer exists.')
                return redirect('dashboard:manage_bookings')
        else:
            tables = _booking_tables(booking)          # tables chosen in the old flow
            if not tables:
                messages.error(request, f'Choose at least one table for booking #{booking.pk}.')
                return redirect('dashboard:manage_bookings')

        seats = sum(t.capacity for t in tables)
        if seats < booking.guests:
            messages.error(
                request,
                f'Selected {seats} seats, but {booking.guests} guests are coming. Choose more tables.')
            return redirect('dashboard:manage_bookings')

        conflicts = _slot_conflicts(booking)
        held = [f'Table {t.number} ({conflicts[t.id]})' for t in tables if t.id in conflicts]
        if held and not force:
            messages.error(
                request,
                'Cannot approve: ' + '; '.join(held)
                + '. Choose different tables, open the held table first, or approve anyway.')
            return redirect('dashboard:manage_bookings')

        booking.table = tables[0]
        booking.status = 'approved'
        booking.decided_at = timezone.now()
        booking.save(update_fields=['status', 'table', 'decided_at'])
        booking.extra_tables.set(tables[1:])

    _email_booking(
        booking,
        f'Booking #{booking.pk} - Confirmed',
        f"Hi {_booking_name(booking)},\n\n"
        f"Good news! Your table booking is confirmed.\n\n"
        f"{_booking_details_text(booking)}\n\n"
        f"We look forward to seeing you.\n\n"
        f"Chandru Restaurant",
    )
    messages.success(request, f'Booking #{booking.pk} approved: {_tables_text(tables)}.')
    return redirect('dashboard:manage_bookings')


@admin_required
@require_POST
def reject_booking(request, pk):
    """pending -> rejected. The reason is saved (if the model has a reason field) and emailed."""
    reason = (request.POST.get('reason') or '').strip()[:200]

    with transaction.atomic():
        # No select_related('table'): a pending booking may have no table yet
        booking = get_object_or_404(TableBooking.objects.select_for_update(), pk=pk)

        if booking.status != 'pending':
            messages.error(request, f'Booking #{booking.pk} is already {booking.status}.')
            return redirect('dashboard:manage_bookings')

        booking.status = 'rejected'
        booking.decided_at = timezone.now()
        fields = ['status', 'decided_at']

        model_fields = {f.name for f in TableBooking._meta.get_fields()}
        for reason_field in ('reject_reason', 'rejection_reason', 'admin_note', 'reason'):
            if reason_field in model_fields:
                setattr(booking, reason_field, reason)
                fields.append(reason_field)
                break

        booking.save(update_fields=fields)

    _email_booking(
        booking,
        f'Booking #{booking.pk} - Could not be accepted',
        f"Hi {_booking_name(booking)},\n\n"
        f"Sorry, we could not accept your table booking.\n"
        + (f"Reason: {reason}\n" if reason else "")
        + f"\n{_booking_details_text(booking)}\n\n"
        f"Please try another time. We are sorry for the trouble.\n\n"
        f"Chandru Restaurant",
    )
    messages.success(request, f'Booking #{booking.pk} rejected.')
    return redirect('dashboard:manage_bookings')
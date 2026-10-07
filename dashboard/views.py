import csv
from datetime import timedelta
from functools import wraps
from django.http import HttpResponse
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Sum, F, Count
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
from booking.availability import booking_window, is_table_busy_now, release_table, table_status_map, upcoming_status_map
from orders.models import Order, OrderItem, Coupon

logger = logging.getLogger(__name__)

# Orders that have been paid for (used for sales totals). Sales must not drop
# to zero once the kitchen/server/delivery move an order past "confirmed".
PAID_STATUSES = ['confirmed', 'preparing', 'ready', 'out_for_delivery', 'delivered']


@admin_required
def admin_dashboard(request):
    low_stock_qs = GroceryItem.objects.filter(quantity__lte=F('low_stock_limit'))
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
        'total_grocery': GroceryItem.objects.count(),
        'low_stock_count': low_stock_qs.count(),
        'low_stock_items': low_stock_qs[:5],
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


@admin_required
def manage_orders(request):
    orders = Order.objects.select_related('user', 'table', 'delivery_person').prefetch_related('items__food_item').order_by('-created_at')
    tables = list(Table.objects.all().order_by('number'))

    # table_id -> order id of the order holding it (served orders hold it until "Customer Left")
    held = {
        o.table_id: o.id
        for o in Order.objects.filter(table__isnull=False, table_released=False)
                              .exclude(status='cancelled')
    }

    tables_info = [{
        'id': t.id,
        'number': t.number,
        'busy': is_table_busy_now(t.id),
        'held_by': held.get(t.id),      # order id, or None
    } for t in tables]

    free_tables = [t for t in tables if not is_table_busy_now(t.id) and t.id not in held]
    return render(request, 'dashboard/manage_orders.html', {
        'orders': orders,
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
    Pending (not yet approved) and cancelled orders only get the payment marked."""
    order = get_object_or_404(Order, pk=pk, payment_method='cash')
    order.payment_status = 'paid'
    fields = ['payment_status']
    if order.status in ('confirmed', 'preparing', 'ready', 'out_for_delivery'):
        order.status = 'delivered'
        fields.append('status')
    order.save(update_fields=fields)
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
    """pending -> confirmed (appears on the Kitchen board). Uses up the grocery stock.
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

        # Grocery this order uses, from each food item's recipe
        need = {}
        for oi in order.items.select_related('food_item').prefetch_related('food_item__recipe'):
            for r in oi.food_item.recipe.all():
                need[r.grocery_id] = need.get(r.grocery_id, Decimal('0')) + r.qty_per_serving * oi.quantity

        stock = {g.id: g for g in GroceryItem.objects.select_for_update().filter(id__in=need.keys())}
        short = [
            f'{stock[gid].name} (need {qty.quantize(Decimal("0.01"))}, have {stock[gid].quantity} {stock[gid].unit})'
            for gid, qty in need.items() if gid in stock and stock[gid].quantity < qty
        ]
        if short and not force:
            return {'ok': False, 'short': True, 'error': 'Not enough stock: ' + ', '.join(short)}, 409

        # Use up the stock (never below zero)
        for gid, qty in need.items():
            g = stock.get(gid)
            if g:
                g.quantity = max(Decimal('0'), g.quantity - qty.quantize(Decimal('0.01')))
                g.save(update_fields=['quantity', 'updated_at'])

        order.status = 'confirmed'
        order.save(update_fields=fields)
    return {'ok': True, 'status': 'confirmed'}, 200


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
                + ("\nYour online payment will be refunded to you.\n" if refund else "")
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

    return {'ok': True, 'status': 'cancelled', 'refund': refund}, 200


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
    order = get_object_or_404(
        Order.objects.prefetch_related('items__food_item__recipe__grocery'), pk=pk)

    # Grocery this order needs, from each food item's recipe
    need = {}
    no_recipe = []
    for oi in order.items.all():
        recipe = list(oi.food_item.recipe.all())
        if not recipe:
            no_recipe.append(oi.food_item.name)
        for r in recipe:
            need[r.grocery] = need.get(r.grocery, Decimal('0')) + r.qty_per_serving * oi.quantity

    requirements = [{
        'name': g.name,
        'unit': g.unit,
        'need': float(qty),
        'have': float(g.quantity),
        'short': g.quantity < qty,
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

    groceries = [{
        'name': g.name,
        'qty': float(g.quantity),
        'unit': g.unit,
        'low': g.quantity <= g.low_stock_limit,
    } for g in GroceryItem.objects.all().order_by('name')]

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

    context = {
        'range': rng,
        'range_label': labels[rng],
        'ranges': ranges,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'recent_orders': orders.select_related('user').order_by('-created_at')[:50],
        'online_revenue': total(online_orders),
        'online_count': online_orders.count(),
        'cash_revenue': total(cash_collected_orders),
        'cash_count': cash_collected_orders.count(),
        'cash_pending_amount': total(cash_pending_orders),
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

    grocery_items = GroceryItem.objects.all()
    low_stock_count = GroceryItem.objects.filter(quantity__lte=F('low_stock_limit')).count()

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


@store_or_kitchen_required
@require_POST
def kitchen_request_stock(request, pk):
    """Chef reports an item as out of stock -> Store Manager gets an alert."""
    from .models import StockRequest
    item = get_object_or_404(GroceryItem, pk=pk)
    if StockRequest.objects.filter(item=item, status='pending').exists():
        return JsonResponse({'ok': False, 'error': 'Already reported. Waiting for store to restock.'}, status=400)
    note = (request.POST.get('note') or '').strip()[:120]
    StockRequest.objects.create(item=item, note=note, requested_by=request.user)
    msg = f'🚫 Kitchen → Store: {item.name} is out of stock ({item.quantity} {item.unit} left). Please restock.'
    if note:
        msg += f' Note: {note}'
    Notification.objects.create(kind='stock_out', message=msg[:255], created_by=request.user)
    return JsonResponse({'ok': True, 'name': item.name})


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


# ---- Order status flow (server -> kitchen -> server / delivery) ----
#
#   pending -> confirmed                         (server: "Send to Kitchen", see server_send_to_kitchen)
#   confirmed -> preparing -> ready              (kitchen)
#   ready -> delivered                           (server: dine-in and takeaway)
#   ready -> out_for_delivery -> delivered       (delivery staff: home delivery only)
#
# Which status moves each role is allowed to make. Admin can make any move.
ROLE_TRANSITIONS = {
    'kitchen': {('confirmed', 'preparing'), ('preparing', 'ready')},
    'server': {('ready', 'delivered')},
    'delivery': {('ready', 'out_for_delivery'), ('out_for_delivery', 'delivered')},
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
    if role == 'delivery':
        return 'dashboard:delivery_dashboard'
    if role == 'server':
        return 'dashboard:server_dashboard'
    if role == 'admin' and new_status in ('delivered', 'out_for_delivery'):
        return 'dashboard:server_dashboard'
    return 'dashboard:kitchen_dashboard'


def _transition_error(order, user, role, new_status):
    """Returns a message if this staff member may not move the order to new_status, else None."""
    if role != 'admin':
        if (order.status, new_status) not in ROLE_TRANSITIONS.get(role, set()):
            if new_status == 'delivered':
                return 'Only the server (or delivery staff for home delivery) can complete an order.'
            return f'Order #{order.id} cannot be moved to "{new_status}" from "{order.status}".'
        if role == 'server' and order.is_delivery:
            return f'Order #{order.id} is a home delivery. Delivery staff will handle it.'
        if role == 'delivery' and not order.is_delivery:
            return f'Order #{order.id} is not a home delivery order.'
        if (role == 'delivery' and order.status == 'out_for_delivery'
                and order.delivery_person_id not in (None, user.id)):
            return f'Order #{order.id} was picked up by another delivery person.'

    if new_status == 'out_for_delivery' and not order.is_delivery:
        return 'Only home delivery orders can go out for delivery.'
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


@_role_required('admin', 'kitchen', 'server', 'delivery')
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
            if new_status == 'out_for_delivery':
                order.delivery_person = request.user
                fields.append('delivery_person')
            order.save(update_fields=fields)

    if error:
        messages.error(request, error)
        return redirect(_board_for(role, new_status))

    name = order.user.get_full_name() or order.user.username

    if new_status == 'out_for_delivery':
        Notification.objects.create(
            kind='out_for_delivery',
            order=order,
            created_by=request.user,
            is_read=True,
            message=f'Order #{order.id} picked up for delivery by {request.user.username}'[:255],
        )
        _email_customer(
            order,
            f'Order #{order.id} - Out for delivery',
            f"Hi {name},\n\n"
            f"Good news! Your order #{order.id} is on the way. 🛵\n\n"
            f"Thank you for choosing Chandru Restaurant!",
        )

    elif new_status == 'delivered':
        if order.is_delivery:
            where = 'the customer address'
        elif order.table:
            where = f'Table {order.table.number}'
        else:
            where = 'takeaway counter'

        Notification.objects.create(
            kind='served',
            order=order,
            created_by=request.user,
            message=f'Order #{order.id} delivered to {where} by {request.user.username}'[:255],
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
    """Use up grocery stock for an order (never below zero). Used when an admin moves an order
    that never went through Approve (pending or rejected) straight to an active status."""
    need = {}
    for oi in order.items.select_related('food_item').prefetch_related('food_item__recipe'):
        for r in oi.food_item.recipe.all():
            need[r.grocery_id] = need.get(r.grocery_id, Decimal('0')) + r.qty_per_serving * oi.quantity
    stock = {g.id: g for g in GroceryItem.objects.select_for_update().filter(id__in=need.keys())}
    for gid, qty in need.items():
        g = stock.get(gid)
        if g:
            g.quantity = max(Decimal('0'), g.quantity - qty.quantize(Decimal('0.01')))
            g.save(update_fields=['quantity', 'updated_at'])


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

        if status == 'out_for_delivery' and not order.is_delivery:
            return JsonResponse(
                {'ok': False, 'error': 'Only home delivery orders can go out for delivery.'}, status=400)

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
    return JsonResponse({'ok': True, 'status': status, 'stock_used': needs_stock})


# ---- Server ----
def _orderable_food_items():
    """Menu items the server can add to an order (hides unavailable ones if the model has that flag)."""
    qs = FoodItem.objects.all()
    if any(f.name == 'is_available' for f in FoodItem._meta.get_fields()):
        qs = qs.filter(is_available=True)
    return qs.order_by('name')


@server_required
def server_dashboard(request):
    # Customer orders waiting for the server. Server sends them to the kitchen (or rejects).
    # Online orders whose payment is not finished yet are not shown.
    new_orders = (Order.objects.filter(status='pending')
                  .exclude(payment_status='pending')
                  .select_related('table', 'user')
                  .prefetch_related('items__food_item')
                  .order_by('created_at'))

    # Home delivery orders are handled by delivery staff, not the server
    ready_orders = (Order.objects.filter(status='ready').exclude(order_type='delivery')
                    .select_related('table', 'user').order_by('created_at'))
    delivered_today = (Order.objects.filter(status='delivered', created_at__date=timezone.localdate())
                       .exclude(order_type='delivery').order_by('-created_at'))

    # Served, but the customer has not left yet (table still held until "Customer Left")
    dining_orders = (Order.objects.filter(status='delivered', table__isnull=False, table_released=False)
                     .exclude(order_type='delivery')
                     .select_related('table', 'user').order_by('created_at'))

    todays_bookings = TableBooking.objects.filter(
        booking_date=timezone.localdate()
    ).select_related('table').order_by('booking_time')

    context = {
        'new_orders': new_orders,
        'ready_orders': ready_orders,
        'delivered_today': delivered_today,
        'dining_orders': dining_orders,
        'todays_bookings': todays_bookings,
        'tables': _free_tables(),
        'food_items': _orderable_food_items(),
    }
    return render(request, 'dashboard/server.html', context)


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
    """Server taps 'Customer Left': the table of this order (pk = order id) becomes free."""
    order = get_object_or_404(Order, pk=pk)

    if not order.table_id:
        msg, ok = 'This order has no table.', False
    elif order.table_released:
        msg, ok = f'Table #{order.table.number} is already free.', False
    else:
        order.table_released = True
        order.save(update_fields=['table_released'])
        msg, ok = f'Table #{order.table.number} is free now.', True

    payload = {'ok': ok, 'message': msg, 'table': order.table.number if order.table_id else None}
    if not ok:
        payload['error'] = msg
    return JsonResponse(payload, status=200 if ok else 400)


@server_required
@require_POST
def server_take_order(request):
    """Server takes an order at the table. It lands straight on the Kitchen board (status 'confirmed'),
    paid in cash at the counter (cash_pending until the admin marks it received)."""
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
        if is_table_busy_now(table.id):
            return JsonResponse({'ok': False, 'error': f'Table {table.number} is booked right now.'}, status=409)
        # Table still held by another order (customer has not left yet)
        holder = _order_holding_table(table.id)
        if holder:
            return JsonResponse(
                {'ok': False, 'error': f'Table {table.number} is occupied (order #{holder.id}).'}, status=409)

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
    """Polled by the server dashboard so new customer orders and finished orders show up live."""
    # Customer orders waiting for the server
    new_orders = (Order.objects.filter(status='pending')
                  .exclude(payment_status='pending')
                  .select_related('table').order_by('created_at'))
    orders = (Order.objects.filter(status='ready').exclude(order_type='delivery')
              .select_related('table').order_by('created_at'))
    return JsonResponse({
        'new_orders': [
            {'id': o.id, 'table': o.table.number if o.table else None}
            for o in new_orders
        ],
        'orders': [
            {'id': o.id, 'table': o.table.number if o.table else None}
            for o in orders
        ]
    })


# ---- Delivery ----
@_role_required('admin', 'delivery')
def delivery_dashboard(request):
    """Home delivery board: orders ready to pick up, the ones on the way, and today's finished ones.
    A delivery person sees only their own on-the-way / delivered orders. Admin sees all."""
    is_admin = _staff_role(request.user) == 'admin'
    base = (Order.objects.filter(order_type='delivery')
            .select_related('user', 'delivery_person').prefetch_related('items__food_item'))

    ready_orders = base.filter(status='ready').order_by('created_at')
    on_the_way = base.filter(status='out_for_delivery').order_by('created_at')
    delivered_today = base.filter(status='delivered', created_at__date=timezone.localdate()).order_by('-created_at')
    if not is_admin:
        on_the_way = on_the_way.filter(delivery_person=request.user)
        delivered_today = delivered_today.filter(delivery_person=request.user)

    return render(request, 'dashboard/delivery.html', {
        'ready_orders': ready_orders,
        'on_the_way': on_the_way,
        'delivered_today': delivered_today,
    })


@_role_required('admin', 'delivery')
def delivery_poll(request):
    """Polled by the delivery dashboard so a newly ready order shows up live."""
    orders = Order.objects.filter(order_type='delivery', status='ready').order_by('created_at')
    return JsonResponse({
        'orders': [
            {'id': o.id, 'address': o.delivery_address[:80], 'phone': o.delivery_phone}
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
    lines = [
        f"Table: {booking.table.number}",
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


def _booking_clash(booking):
    """Another APPROVED booking on the same table whose time window overlaps this one."""
    start, end = booking_window(booking)
    others = (TableBooking.objects
              .filter(table_id=booking.table_id, status='approved')
              .exclude(pk=booking.pk))
    for other in others:
        if getattr(other, 'released', False):
            continue
        o_start, o_end = booking_window(other)
        if o_start < end and start < o_end:
            return other
    return None


@admin_required
@require_POST
def approve_booking(request, pk):
    """pending -> approved. Fails if another approved booking overlaps on the same table."""
    with transaction.atomic():
        booking = get_object_or_404(
            TableBooking.objects.select_for_update().select_related('table'), pk=pk)

        if booking.status != 'pending':
            messages.error(request, f'Booking #{booking.pk} is already {booking.status}.')
            return redirect('dashboard:manage_bookings')

        clash = _booking_clash(booking)
        if clash:
            messages.error(
                request,
                f'Cannot approve: Table {booking.table.number} already has an approved '
                f'booking (#{clash.pk}) at that time.')
            return redirect('dashboard:manage_bookings')

        booking.status = 'approved'
        booking.save(update_fields=['status'])

    _email_booking(
        booking,
        f'Booking #{booking.pk} - Confirmed',
        f"Hi {_booking_name(booking)},\n\n"
        f"Good news! Your table booking is confirmed.\n\n"
        f"{_booking_details_text(booking)}\n\n"
        f"We look forward to seeing you.\n\n"
        f"Chandru Restaurant",
    )
    messages.success(request, f'Booking #{booking.pk} approved.')
    return redirect('dashboard:manage_bookings')


@admin_required
@require_POST
def reject_booking(request, pk):
    """pending -> rejected. The reason is saved (if the model has a reason field) and emailed."""
    reason = (request.POST.get('reason') or '').strip()[:200]

    with transaction.atomic():
        booking = get_object_or_404(
            TableBooking.objects.select_for_update().select_related('table'), pk=pk)

        if booking.status != 'pending':
            messages.error(request, f'Booking #{booking.pk} is already {booking.status}.')
            return redirect('dashboard:manage_bookings')

        booking.status = 'rejected'
        fields = ['status']

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
        f"Please try another time or table. We are sorry for the trouble.\n\n"
        f"Chandru Restaurant",
    )
    messages.success(request, f'Booking #{booking.pk} rejected.')
    return redirect('dashboard:manage_bookings')
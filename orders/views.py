import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from menu.models import FoodItem
from booking.models import Table
from booking.availability import table_status_map, is_table_busy_now, upcoming_holds, WALKIN_BUFFER
from .models import Order, OrderItem, Coupon

logger = logging.getLogger(__name__)

# Highest quantity of one dish a customer can put in the cart
MAX_QTY = 20

# Tip percentages the cart page offers. Anything else is treated as no tip.
ALLOWED_TIPS = ('5', '10')


def _is_ajax(request):
    """True when the request comes from fetch() with the X-Requested-With header."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _safe_qty(raw, default=1):
    """Turns user input into a quantity between 1 and MAX_QTY.
    Bad input like 'abc' or None gives the default instead of crashing."""
    try:
        qty = int(raw)
    except (TypeError, ValueError):
        qty = default
    return max(1, min(qty, MAX_QTY))


def _add_to_session_cart(cart, item_id, quantity):
    """Adds quantity to the cart dict, never going above MAX_QTY."""
    key = str(item_id)
    cart[key] = min(cart.get(key, 0) + quantity, MAX_QTY)
    return cart


@require_POST
def add_to_cart(request, item_id):
    quantity = _safe_qty(request.POST.get('quantity', 1))

    if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
        if _is_ajax(request):
            return JsonResponse({'success': False, 'error': 'This item is not available.'}, status=400)
        messages.error(request, 'This item is not available.')
        return redirect('menu_list')

    cart = request.session.get('cart', {})
    _add_to_session_cart(cart, item_id, quantity)

    request.session['cart'] = cart
    request.session.modified = True

    # AJAX (menu page side panel): stay on the same page, return JSON
    if _is_ajax(request):
        return JsonResponse(_cart_payload(request, message='Item added to cart!'))

    messages.success(request, 'Item added to cart!')
    return redirect('orders:view_cart')


@require_POST
@login_required
def add_to_order(request, item_id):
    quantity = _safe_qty(request.POST.get('quantity', 1))

    if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
        messages.error(request, 'This item is not available.')
        return redirect('menu_detail', item_id=item_id)

    cart = request.session.get('cart', {})
    _add_to_session_cart(cart, item_id, quantity)

    request.session['cart'] = cart
    request.session.modified = True

    return redirect('orders:confirm_order')


@require_POST
def bulk_add_to_cart(request):
    try:
        data = json.loads(request.body)
        items = data.get('items', [])
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Invalid data'}, status=400)

    if not items or not isinstance(items, list):
        return JsonResponse({'success': False, 'error': 'No items selected'}, status=400)

    cart = request.session.get('cart', {})

    added_count = 0
    for entry in items:
        if not isinstance(entry, dict):
            continue

        # Item id must be a real number, otherwise skip this entry
        try:
            item_id = int(entry.get('id'))
        except (TypeError, ValueError):
            continue

        quantity = _safe_qty(entry.get('quantity', 1))

        if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
            continue

        _add_to_session_cart(cart, item_id, quantity)
        added_count += 1

    request.session['cart'] = cart
    request.session.modified = True

    return JsonResponse({
        'success': True,
        'added_count': added_count,
        'cart_total_items': sum(cart.values())
    })


def select_table(request, table_id):
    """QR code scan lands here. Stores the table in the session and
    sends the customer to the menu to start ordering for that table."""
    table = get_object_or_404(Table, id=table_id)

    # CHANGED: if the table is held by someone else right now, don't attach it
    if is_table_busy_now(table.id, request.user):
        messages.warning(
            request,
            f'Table #{table.number} is booked right now. Our staff will help you with a table.'
        )
        request.session.pop('selected_table', None)
        request.session.modified = True
        return redirect('menu_list')

    request.session['selected_table'] = table.id
    request.session.modified = True
    messages.success(request, f'You are at Table #{table.number}. Add items and checkout!')
    return redirect('menu_list')


# NEW: "Change table" button on the cart page
@require_POST
def clear_table(request):
    """Customer taps 'Change table': forget the QR table so they can scan a new
    one, or the server assigns a table."""
    request.session.pop('selected_table', None)
    request.session.modified = True
    messages.info(request, 'Table removed. Scan the QR on your new table, or our staff will assign one.')
    return redirect('orders:view_cart')


def _get_tables_with_status(user=None):
    """Returns all tables tagged with whether they're currently held.

    A table is hidden/held for 2 hours after it is booked (reservation time or
    dine-in order) and then opens again automatically. The admin can also open
    it early. The customer who holds the table can still use it.

    A table with an upcoming reservation starting within WALKIN_BUFFER is also
    shown as booked, so a walk-in doesn't get seated right before the
    reserved customer arrives.
    """
    now = timezone.now()
    holds = table_status_map()

    # Soonest upcoming booking (not yet started) per table, if it starts
    # within the walk-in buffer window.
    soon_holds = {}
    for h in upcoming_holds():
        if h.kind == 'booking' and h.start - now <= WALKIN_BUFFER:
            current = soon_holds.get(h.table_id)
            if current is None or h.start < current.start:
                soon_holds[h.table_id] = h

    result = []
    for table in Table.objects.all().order_by('number'):
        hold = holds.get(table.id) or soon_holds.get(table.id)
        is_booked = bool(hold) and not (user is not None and user.is_authenticated and hold.user_id == user.id)
        result.append({
            'id': table.id,
            'number': table.number,
            'is_booked': is_booked,
            'free_at': hold.end if is_booked else None,
        })
    return result


def _get_cart_totals(request):
    """Shared helper: computes subtotal, discount and final total for the
    current session cart + any applied coupon. Used by view_cart and confirm_order
    so both stay in sync. The total here does NOT include the tip."""
    cart = request.session.get('cart', {})
    cart_items = []
    subtotal = Decimal('0.00')

    for item_id_str, quantity in cart.items():
        try:
            food_item = FoodItem.objects.get(id=item_id_str, is_available=True)
        except (FoodItem.DoesNotExist, ValueError):
            continue

        item_subtotal = food_item.price * quantity
        subtotal += item_subtotal

        cart_items.append({
            'id': food_item.id,
            'name': food_item.name,
            'price': food_item.price,
            'quantity': quantity,
            'subtotal': item_subtotal,
            'image': food_item.image if hasattr(food_item, 'image') else None,
            'image_url': food_item.image.url if food_item.image else '',
            'is_veg': food_item.is_veg,
        })

    coupon = None
    discount_amount = Decimal('0.00')
    coupon_code = request.session.get('coupon_code')
    if coupon_code:
        coupon = Coupon.objects.filter(code__iexact=coupon_code).first()
        if coupon and coupon.is_valid():
            discount_amount = (
                Decimal(subtotal) * coupon.discount_percent / Decimal('100')
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            # Coupon expired/used up/deactivated since it was applied - drop it silently
            coupon = None
            request.session.pop('coupon_code', None)
            request.session.modified = True

    total = subtotal - discount_amount
    return cart_items, subtotal, coupon, discount_amount, total


def _money(value):
    return f"{Decimal(value):.2f}"


def _cart_payload(request, **extra):
    """JSON used by the menu page side cart panel and the cart page."""
    cart_items, subtotal, coupon, discount_amount, total = _get_cart_totals(request)
    data = {
        'success': True,
        'items': [
            {
                'id': i['id'],
                'name': i['name'],
                'price': _money(i['price']),
                'quantity': i['quantity'],
                'subtotal': _money(i['subtotal']),
                'image_url': i['image_url'],
                'is_veg': i['is_veg'],
            }
            for i in cart_items
        ],
        'subtotal': _money(subtotal),
        'discount': _money(discount_amount),
        'total': _money(total),
        'count': len(cart_items),
        'total_qty': sum(i['quantity'] for i in cart_items),
    }
    data.update(extra)
    return data


def cart_data(request):
    """Current cart as JSON. The menu page calls this on load to fill the side panel."""
    return JsonResponse(_cart_payload(request))


def _calc_tip(total, tip_percent):
    """Tip on the amount after discount. Rounds half-up to 2 places, the same
    way the cart page JavaScript does, so both show the same paisa."""
    if tip_percent not in ALLOWED_TIPS:
        return Decimal('0.00')
    tip = Decimal(total) * Decimal(tip_percent) / Decimal('100')
    return tip.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def view_cart(request):
    cart_items, subtotal, coupon, discount_amount, total = _get_cart_totals(request)

    # "You may also like": other available dishes that are not in the cart yet
    cart_ids = [i['id'] for i in cart_items]
    suggestions = list(
        FoodItem.objects.orderable().exclude(id__in=cart_ids).order_by('?')[:4]
    )

    # NEW: table from the QR scan (None if the customer did not scan one)
    selected_table = None
    table_id = request.session.get('selected_table')
    if table_id:
        selected_table = Table.objects.filter(id=table_id).first()

    context = {
        'cart_items': cart_items,
        'subtotal': subtotal,
        'coupon': coupon,
        'discount_amount': discount_amount,
        'total': total,
        'suggestions': suggestions,
        'selected_table': selected_table,   # NEW
    }
    return render(request, 'orders/cart.html', context)


@require_POST
def update_cart_qty(request, item_id):
    """+ / - buttons on the cart page and the menu side panel. Returns JSON so the page updates live."""
    cart = request.session.get('cart', {})
    key = str(item_id)

    if key not in cart:
        return JsonResponse({'success': False, 'error': 'Item not in cart'}, status=404)

    action = request.POST.get('action')
    if action == 'inc':
        cart[key] = min(cart[key] + 1, MAX_QTY)
    elif action == 'dec':
        cart[key] = max(cart[key] - 1, 1)
    else:
        return JsonResponse({'success': False, 'error': 'Bad action'}, status=400)

    request.session['cart'] = cart
    request.session.modified = True

    cart_items, subtotal, coupon, discount_amount, total = _get_cart_totals(request)
    line = next((i for i in cart_items if i['id'] == item_id), None)

    return JsonResponse(_cart_payload(
        request,
        quantity=cart[key],
        line_subtotal=_money(line['subtotal']) if line else '0.00',
    ))


@require_POST
def apply_coupon(request):
    code = request.POST.get('coupon_code', '').strip()

    if not code:
        messages.error(request, 'Enter a coupon code.')
        return redirect('orders:view_cart')

    coupon = Coupon.objects.filter(code__iexact=code).first()

    if not coupon or not coupon.is_valid():
        messages.error(request, 'Invalid or expired coupon code.')
        return redirect('orders:view_cart')

    request.session['coupon_code'] = coupon.code
    request.session.modified = True
    messages.success(request, f'Coupon "{coupon.code}" applied - {coupon.discount_percent}% off!')
    return redirect('orders:view_cart')


@require_POST
def remove_coupon(request):
    request.session.pop('coupon_code', None)
    request.session.modified = True
    messages.success(request, 'Coupon removed.')
    return redirect('orders:view_cart')


@require_POST
def remove_from_cart(request, item_id):
    cart = request.session.get('cart', {})
    item_id_str = str(item_id)

    if item_id_str in cart:
        del cart[item_id_str]
        request.session['cart'] = cart
        request.session.modified = True
        if not _is_ajax(request):
            messages.success(request, 'Item removed from cart.')

    if _is_ajax(request):
        return JsonResponse(_cart_payload(request))
    return redirect('orders:view_cart')


def _parse_arrival(request):
    """Reads the arrival choice from the cart form.
    Returns a datetime, or None which means 'as soon as possible'."""
    now = timezone.now()
    mode = request.POST.get('arrival_mode', 'asap')

    if mode in ('15', '30', '45', '60'):
        return now + timedelta(minutes=int(mode))

    if mode == 'custom':
        raw = request.POST.get('arrival_custom', '').strip()
        try:
            picked = datetime.strptime(raw, '%H:%M').time()
        except ValueError:
            messages.warning(request, 'Arrival time was not valid, so we marked your order as ASAP.')
            return None

        local_now = timezone.localtime(now)
        arrival = local_now.replace(hour=picked.hour, minute=picked.minute, second=0, microsecond=0)
        if arrival < local_now:
            messages.warning(request, 'That arrival time has already passed, so we marked your order as ASAP.')
            return None
        return arrival

    return None


@login_required
def confirm_order(request):
    """Creates the order as PENDING (payment_status 'pending') and sends the
    user to the mock Razorpay checkout page."""
    cart = request.session.get('cart', {})

    if not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:view_cart')

    order_items_data = []
    for item_id_str, quantity in cart.items():
        try:
            food_item = FoodItem.objects.get(id=item_id_str, is_available=True)
        except (FoodItem.DoesNotExist, ValueError):
            continue
        order_items_data.append((food_item, quantity, food_item.price))

    if not order_items_data:
        messages.error(request, 'No valid items in your cart.')
        return redirect('orders:view_cart')

    _, subtotal, coupon, discount_amount, total = _get_cart_totals(request)

    # Optional tip (5% or 10% of the amount after discount). Always calculated
    # here on the server, the browser only sends the percent.
    tip_percent = request.POST.get('tip_percent', '0')
    total = Decimal(total) + _calc_tip(total, tip_percent)

    # Order options from the cart page (a plain GET, e.g. "Order now" from a
    # dish page, simply uses the defaults: dine-in, ASAP, no note, no tip)
    order_type = request.POST.get('order_type', 'dine_in')
    if order_type not in ('dine_in', 'takeaway', 'delivery'):
        order_type = 'dine_in'
    arrival_time = _parse_arrival(request)
    note = request.POST.get('note', '').strip()[:255]

    # Home delivery needs an address and a phone number the rider can call
    delivery_address = ''
    delivery_phone = ''
    if order_type == 'delivery':
        delivery_address = request.POST.get('delivery_address', '').strip()[:500]
        delivery_phone = request.POST.get('delivery_phone', '').strip()
        digits = ''.join(ch for ch in delivery_phone if ch.isdigit())
        if len(delivery_address) < 10:
            messages.error(request, 'Please enter your full delivery address.')
            return redirect('orders:view_cart')
        if not 10 <= len(digits) <= 13:
            messages.error(request, 'Please enter a valid phone number for delivery.')
            return redirect('orders:view_cart')
        delivery_phone = delivery_phone[:15]

    # Customers no longer pick a table in the cart. A table is only attached
    # when they scanned a table QR code and are dining in; otherwise the server
    # (or admin) assigns one. Take away orders never get a table.
    table_id = request.session.get('selected_table') if order_type == 'dine_in' else None
    selected_table = None
    if table_id:
        selected_table = Table.objects.filter(id=table_id).first()

        # Guard: if the table got booked between the QR scan and submit, drop it
        if selected_table and is_table_busy_now(selected_table.id, request.user):
            messages.warning(request, f'Table #{selected_table.number} is booked now - our staff will assign you a table.')
            selected_table = None

    # All-or-nothing: if anything fails, no half-created order is left behind
    with transaction.atomic():
        order = Order.objects.create(
            user=request.user,
            table=selected_table,
            status='pending',
            coupon=coupon,
            discount_amount=discount_amount,
            total_amount=total,
            order_type=order_type,
            arrival_time=arrival_time,
            note=note,
            delivery_address=delivery_address,
            delivery_phone=delivery_phone,
        )

        for food_item, quantity, price in order_items_data:
            OrderItem.objects.create(
                order=order,
                food_item=food_item,
                quantity=quantity,
                price=price,
            )

        if coupon:
            # F() makes the +1 happen in the database, safe when two people use the coupon together
            Coupon.objects.filter(pk=coupon.pk).update(times_used=F('times_used') + 1)

    request.session['cart'] = {}
    request.session.pop('selected_table', None)
    request.session.pop('coupon_code', None)
    request.session.modified = True

    return redirect('orders:initiate_payment', order_id=order.id)


@login_required
def initiate_payment(request, order_id):
    """Shows the mock Razorpay checkout page. Only for orders that are still
    waiting for payment (status 'pending' AND payment_status 'pending')."""
    order = get_object_or_404(
        Order, id=order_id, user=request.user, status='pending', payment_status='pending'
    )
    return render(request, 'orders/razorpay_checkout.html', {'order': order})


@login_required
@require_POST
def payment_success(request, order_id):
    """Called when the mock checkout 'completes'. The order stays PENDING:
    it now waits for the server/admin to approve (-> 'confirmed', appears on the
    Kitchen board) or reject (-> 'cancelled')."""
    order = get_object_or_404(
        Order, id=order_id, user=request.user, status='pending', payment_status='pending'
    )

    payment_method = request.POST.get('payment_method', 'online')
    if payment_method not in ('online', 'cash'):
        payment_method = 'online'

    # status is NOT changed here - approval moves it to 'confirmed'
    order.payment_method = payment_method
    order.payment_status = 'cash_pending' if payment_method == 'cash' else 'paid'
    order.save(update_fields=['payment_method', 'payment_status'])

    if request.user.email:
        item_lines = "\n".join(
            f"- {oi.food_item.name} x{oi.quantity} = Rs.{oi.price * oi.quantity}"
            for oi in order.items.all()
        )
        discount_line = f"\nDiscount ({order.coupon.code}): -Rs.{order.discount_amount}\n" if order.coupon else ""

        if payment_method == 'cash' and order.is_delivery:
            payment_line = f"\nPayment: Cash on Delivery (Rs.{order.total_amount} to be paid to the delivery person)\n"
        elif payment_method == 'cash':
            payment_line = f"\nPayment: Cash at Restaurant (Rs.{order.total_amount} to be paid on arrival)\n"
        else:
            payment_line = f"\nPayment: Paid Online (Rs.{order.total_amount})\n"

        try:
            send_mail(
                subject=f'Order Received - Order #{order.id}',
                message=(
                    f"Hi {request.user.get_full_name() or request.user.username},\n\n"
                    f"We have received your order #{order.id}. "
                    f"It will go to the kitchen as soon as our team approves it.\n\n"
                    f"{item_lines}\n"
                    f"{discount_line}"
                    f"{payment_line}\n"
                    f"Total: Rs.{order.total_amount}\n\n"
                    f"Thank you for ordering with Chandru Restaurant!"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[request.user.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception('Could not send order received email for order %s', order.id)

    if payment_method == 'cash' and order.is_delivery:
        messages.success(request, 'Order placed! Waiting for restaurant approval. Please pay in cash to the delivery person.')
    elif payment_method == 'cash':
        messages.success(request, 'Order placed! Waiting for restaurant approval. Please pay in cash at the restaurant.')
    else:
        messages.success(request, 'Order placed! Waiting for restaurant approval.')

    return redirect('orders:order_history')


def _extract_reason(message):
    """Messages look like 'Order #444 rejected: Out of stock'. Return the part after the colon."""
    marker = 'rejected:'
    if marker in message:
        return message.split(marker, 1)[1].strip()
    return ''


def _rejection_reasons(order_ids):
    """Returns {order_id: reason} from the 'rejected' Notification rows.
    The import stays inside the function on purpose: dashboard.models may import
    from orders, and a top-level import here could cause a circular import."""
    from dashboard.models import Notification

    reasons = {}
    if not order_ids:
        return reasons

    notes = (
        Notification.objects
        .filter(order_id__in=order_ids, kind='rejected')
        .order_by('-created_at')
    )
    for n in notes:
        reasons.setdefault(n.order_id, _extract_reason(n.message))
    return reasons


@login_required
def order_history(request):
    orders = list(
        Order.objects.filter(user=request.user).prefetch_related('items__food_item')
    )

    cancelled_ids = [o.id for o in orders if o.status == 'cancelled']

    # Rejection reasons, saved by the reject view as Notification rows
    reasons = _rejection_reasons(cancelled_ids)

    # Suggestions: other available dishes, preferring the same category
    pool = FoodItem.objects.filter(is_available=True)

    for order in orders:
        order.reject_reason = ''
        order.suggestions = []
        if order.status != 'cancelled':
            continue
        order.reject_reason = reasons.get(order.id, '')
        ordered = [i.food_item for i in order.items.all()]
        ordered_ids = [f.pk for f in ordered]
        alt = pool.exclude(pk__in=ordered_ids)
        if ordered:
            same_cat = alt.filter(category_id__in={f.category_id for f in ordered})
            if same_cat.exists():
                alt = same_cat
        order.suggestions = list(alt[:3])

    return render(request, 'orders/order_history.html', {'orders': orders})


@login_required
def order_status_poll(request):
    """Light JSON feed used by My Orders to show live status changes."""
    orders = list(Order.objects.filter(user=request.user).order_by('-created_at')[:60])
    cancelled_ids = [o.id for o in orders if o.status == 'cancelled']
    reasons = _rejection_reasons(cancelled_ids)

    return JsonResponse({
        'orders': [
            {'id': o.id, 'status': o.status, 'reason': reasons.get(o.id, '')}
            for o in orders
        ]
    })
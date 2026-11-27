import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone
from menu.models import FoodItem, Option
from booking.models import Table, TableSession
from booking.availability import (
    table_available_seats, table_status_map, is_table_reserved_now,
    upcoming_holds, WALKIN_BUFFER,
)
from dashboard.models import Notification
from .cart_state import QR_SESSION_COOKIE, clear_cart, get_cart, save_cart
from .models import Bill, Coupon, Order, OrderItem, PAYMENT_HOLD_MINUTES
from .services import refund_razorpay_payment

# Highest quantity of one dish a customer can put in the cart
MAX_QTY = 20

# Tip percentages the cart page offers. Anything else is treated as no tip.
ALLOWED_TIPS = ('5', '10')

# A dine-in table session older than this is treated as finished, even if the
# staff forgot to close the table. This stops a customer ordering "again" from home
# the next day.
TABLE_SESSION_MAX_HOURS = 4

# CHANGED (guests-all): most guests allowed when the customer has NO table attached
# (no QR scan). The staff assign the table later. Same as MAX_GUESTS in booking/forms.py.
MAX_GUESTS_NO_TABLE = 30
CUSTOMER_CANCELLATION_REASONS = (
    'Emergency Situation',
    'Ordered by Mistake',
    'Duplicate Order',
    'Long Waiting Time',
    'Change of Plans',
)
CUSTOMER_CANCELLABLE_STATUSES = ('pending', 'confirmed')


def _is_ajax(request):
    """True when the request comes from fetch() with the X-Requested-With header."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _forget_table(request):
    """Removes the QR table from this browser session."""
    request.session.pop('table_session_id', None)
    request.session.pop('selected_table', None)
    request.session.modified = True


def _active_table_session(request):
    """The dine-in table session of this browser, or None.

    The session ends when staff close the table (TableSession.close()) or after
    TABLE_SESSION_MAX_HOURS. When it has ended, the table is also removed from this
    browser, so a later order (or "Order again") gets no table until the customer
    scans the QR again.

    A group seated by the server (a session that has SeatAllocation rows) has no
    time limit here: it ends only when staff mark the group as departed."""
    cutoff = timezone.now() - timedelta(hours=TABLE_SESSION_MAX_HOURS)
    token = request.COOKIES.get(QR_SESSION_COOKIE)
    table_session = None
    if token:
        try:
            token = uuid.UUID(token)
        except (TypeError, ValueError, AttributeError):
            token = None
        if token is not None:
            table_session = (
                TableSession.objects.select_related('table')
                .filter(
                    qr_token=token,
                    is_active=True,
                    closed_at__isnull=True,
                    departed_at__isnull=True,
                    started_at__gte=cutoff,
                )
                .exclude(bills__is_billed=True)
                .exclude(bills__is_closed=True)
                .first()
            )

    session_id = request.session.get('table_session_id')
    if table_session is None and session_id:
        table_session = (
            TableSession.objects.select_related('table')
            .filter(id=session_id, is_active=True)
            .filter(Q(started_at__gte=cutoff) | Q(allocations__isnull=False))
            .exclude(bills__is_billed=True)
            .exclude(bills__is_closed=True)
            .distinct()
            .first()
        )
    if table_session is None:
        _forget_table(request)
    return table_session


def _current_table(request):
    """(table, table_session) for the order this browser is about to place.

    1. The active TableSession of the QR scan (normal case).
    2. Backup: if there is no active session (for example the QR scan was handled
       by another view), use the table of this browser's latest dine-in order whose
       customer has not left yet (table_released=False, not cancelled). This is what
       makes "Order again" from My Orders go to the same table.
    Returns (None, None) when neither is found."""
    table_session = _active_table_session(request)
    if table_session:
        return table_session.table, table_session

    previous = (
        _my_orders(request)
        .filter(order_type='dine_in', table__isnull=False, table_released=False)
        .exclude(status='cancelled')
        .select_related('table', 'table_session')
        .order_by('-id')
        .first()
    )
    if previous:
        # A group seated by the server that has already left must not get a table
        # again through "Order again" (its seats are released after cleaning).
        prev_session = previous.table_session
        if prev_session is not None and not prev_session.is_active:
            return None, None
        return previous.table, previous.table_session
    return None, None


# A fresh QR session or an unassigned dine-in order needs an explicit guest count.
# Known QR sessions and server-seated sessions already have their count.
def _asks_guests(table, table_session):
    """True when the cart page should ask for the guest count."""
    if table is None:
        return True
    return not (
        table_session is not None
        and (
            table_session.guest_count is not None
            or table_session.allocations.exists()
        )
    )


def _known_guests(table, table_session=None):
    """Guest count already assigned to this seating session, if any."""
    if table_session is not None:
        return table_session.guest_count or (
            table_session.guests if table_session.allocations.exists() else None
        )
    value = (Order.objects.visible_to_staff()
             .filter(table_id=table.id, table_released=False)
             .exclude(status='cancelled')
             .exclude(table_session__source=TableSession.SOURCE_WALKIN)
             .aggregate(m=Max('guests'))['m'])
    return value or None


def _my_orders(request):
    """Only the orders that this browser session placed (or added with Track Order).
    There is no customer login, so a customer can never see anybody else's order."""
    ids = request.session.get('guest_order_ids', [])
    return Order.objects.filter(id__in=ids, user__isnull=True)


def _expire_stale_orders(request):
    """Cancels this customer's online orders that were not paid within
    PAYMENT_HOLD_MINUTES. Staff screens never show them anyway
    (Order.objects.visible_to_staff()), this just cleans them up."""
    for order in _my_orders(request).stale_unpaid():
        order.expire_if_unpaid()


def _clean_phone(raw):
    """Keeps the digits only and returns the last 10. Empty string when it is too short."""
    digits = ''.join(ch for ch in str(raw) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ''


def _safe_qty(raw, default=1):
    """Turns user input into a quantity between 1 and MAX_QTY.
    Bad input like 'abc' or None gives the default instead of crashing."""
    try:
        qty = int(raw)
    except (TypeError, ValueError):
        qty = default
    return max(1, min(qty, MAX_QTY))


def _parse_line(key):
    """'12' -> (12, []), '12:5-7' -> (12, [5, 7]). A cart line is a dish plus its chosen options."""
    item_part, _, option_part = str(key).partition(':')
    try:
        item_id = int(item_part)
    except ValueError:
        return None, []
    option_ids = [int(p) for p in option_part.split('-') if p.isdigit()]
    return item_id, option_ids


def _line_key(item_id, option_ids):
    ids = sorted(set(option_ids))
    if not ids:
        return str(item_id)
    return str(item_id) + ':' + '-'.join(str(i) for i in ids)


def _load_options(food_item, option_ids):
    """Option rows for a cart line. Only options that still belong to the dish and are available."""
    if not option_ids:
        return []
    found = {
        o.id: o
        for o in Option.objects.filter(
            id__in=option_ids, group__food_item=food_item, is_available=True
        )
    }
    return [found[i] for i in option_ids if i in found]


def _validate_options(food_item, raw_ids):
    """Checks the chosen options against the dish's option groups.
    Returns (clean_option_ids, error_message). The message is empty when all is fine."""
    chosen = set()
    for raw in raw_ids:
        try:
            chosen.add(int(raw))
        except (TypeError, ValueError):
            return [], 'Invalid option.'

    clean = []
    known = set()
    for group in food_item.option_groups.prefetch_related('options'):
        group_options = [o for o in group.options.all() if o.is_available]
        group_ids = {o.id for o in group_options}
        known |= group_ids
        picked = sorted(chosen & group_ids)

        if group.group_type == 'single' and len(picked) > 1:
            return [], 'Pick only one for ' + group.title + '.'
        if group.group_type == 'multi' and group.max_select and len(picked) > group.max_select:
            return [], 'Pick at most ' + str(group.max_select) + ' for ' + group.title + '.'
        if group.is_required and group_options and not picked:
            return [], 'Please choose an option for ' + group.title + '.'
        clean.extend(picked)

    if chosen - known:
        return [], 'One of the chosen options is not available.'
    return clean, ''


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

    # Chosen options (Choose Your Side, Add-ons ...). Empty for plain dishes.
    food_item = FoodItem.objects.get(id=item_id)
    option_ids, option_error = _validate_options(food_item, request.POST.getlist('options'))
    if option_error:
        if _is_ajax(request):
            return JsonResponse({'success': False, 'error': option_error}, status=400)
        messages.error(request, option_error)
        return redirect('menu_list')

    cart = get_cart(request)
    _add_to_session_cart(cart, _line_key(item_id, option_ids), quantity)

    save_cart(request, cart)

    # AJAX (menu page side panel): stay on the same page, return JSON
    if _is_ajax(request):
        return JsonResponse(_cart_payload(request, message='Item added to cart!'))

    messages.success(request, 'Item added to cart!')
    return redirect('orders:view_cart')


@require_POST
def add_to_order(request, item_id):
    """'Order now' from a dish page. Adds the dish to the cart and opens the cart
    page, where the customer picks tip / order type and presses
    Confirm. No login is needed to order."""
    quantity = _safe_qty(request.POST.get('quantity', 1))

    if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
        messages.error(request, 'This item is not available.')
        return redirect('item_detail', item_id=item_id)

    cart = get_cart(request)
    _add_to_session_cart(cart, item_id, quantity)

    save_cart(request, cart)

    return redirect('orders:view_cart')


@require_POST
def bulk_add_to_cart(request):
    try:
        data = json.loads(request.body)
        items = data.get('items', [])
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Invalid data'}, status=400)

    if not items or not isinstance(items, list):
        return JsonResponse({'success': False, 'error': 'No items selected'}, status=400)

    cart = get_cart(request)

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

        food_item = FoodItem.objects.filter(id=item_id, is_available=True).first()
        if food_item is None:
            continue

        # A dish that needs a choice (required option group) cannot be bulk added
        _, option_error = _validate_options(food_item, [])
        if option_error:
            continue

        _add_to_session_cart(cart, item_id, quantity)
        added_count += 1

    save_cart(request, cart)

    return JsonResponse({
        'success': True,
        'added_count': added_count,
        'cart_total_items': sum(cart.values())
    })


def select_table(request, table_id):
    """Create one QR seating session per scan and bind this browser to its token."""
    table = get_object_or_404(Table, id=table_id)
    table_session = None
    reserved = False
    cutoff = timezone.now() - timedelta(hours=TABLE_SESSION_MAX_HOURS)
    with transaction.atomic():
        table = Table.objects.select_for_update().get(id=table.id)
        reserved = is_table_reserved_now(table.id, request.user)
        if not reserved:
            now = timezone.now()
            stale_sessions = TableSession.objects.filter(
                table=table,
                source=TableSession.SOURCE_QR,
                is_active=True,
                started_at__lt=cutoff,
                allocations__isnull=True,
            )
            stale_sessions.update(is_active=False, closed_at=now)
            Bill.objects.filter(table_session__in=stale_sessions).update(
                is_closed=True, closed_at=now
            )

            # Availability is rechecked while holding the table lock. A scan with
            # no remaining seats still gets its own session, but cannot order.
            available_at_scan = table_available_seats(table)
            table_session = TableSession.objects.create(
                table=table,
                source=TableSession.SOURCE_QR,
                qr_token=uuid.uuid4(),
            )
            Bill.objects.create(table_session=table_session)
        else:
            available_at_scan = 0

    if reserved:
        _forget_table(request)
        response = redirect('home')
        response.delete_cookie(QR_SESSION_COOKIE)
        messages.warning(
            request,
            f'Table #{table.number} is reserved right now. Our staff will help you with a table.'
        )
        return response

    assert table_session is not None
    request.session['selected_table'] = table.id
    request.session['table_session_id'] = table_session.id
    request.session.modified = True
    if available_at_scan:
        messages.success(request, f'You are at Table #{table.number}. Tap "View Full Menu" to start ordering!')
    else:
        messages.warning(request, f'No seats available at Table #{table.number}.')
    response = redirect('qr_welcome')
    response.set_cookie(
        QR_SESSION_COOKIE,
        str(table_session.qr_token),
        max_age=TABLE_SESSION_MAX_HOURS * 60 * 60,
        httponly=True,
        secure=request.is_secure(),
        samesite='Lax',
    )
    return response


# "Change table" button on the cart page
@require_POST
def clear_table(request):
    """Forget the current QR group before a new table is scanned."""
    clear_cart(request)
    _forget_table(request)
    messages.info(request, 'Table removed. Scan the QR on your new table, or our staff will assign one.')
    response = redirect('orders:view_cart')
    response.delete_cookie(QR_SESSION_COOKIE)
    return response


def _get_tables_with_status(user=None):
    """Returns all tables tagged with whether they're currently held.

    A held table stays held until the admin opens it (see booking/availability.py).
    The customer who holds the table can still use it.

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
    """Shared helper: computes the cart lines, subtotal and total for the
    current session cart. Used by view_cart and confirm_order so both stay in sync.
    There is no coupon any more, so total == subtotal. The total does NOT include the tip."""
    cart = get_cart(request)
    cart_items = []
    subtotal = Decimal('0.00')

    for line_key, quantity in cart.items():
        item_id, option_ids = _parse_line(line_key)
        if item_id is None:
            continue
        try:
            food_item = FoodItem.objects.get(id=item_id, is_available=True)
        except FoodItem.DoesNotExist:
            continue

        options = _load_options(food_item, option_ids)
        unit_price = food_item.price + sum((o.extra_price for o in options), Decimal('0.00'))
        item_subtotal = unit_price * quantity
        subtotal += item_subtotal

        cart_items.append({
            'id': food_item.id,
            'line': line_key,
            'name': food_item.name,
            'price': unit_price,
            'quantity': quantity,
            'subtotal': item_subtotal,
            'image': food_item.image if hasattr(food_item, 'image') else None,
            'image_url': food_item.image.url if food_item.image else '',
            'is_veg': food_item.is_veg,
            'options_text': ', '.join(o.name for o in options),
        })

    # A coupon applied in an older session is simply forgotten
    if 'coupon_code' in request.session:
        request.session.pop('coupon_code', None)
        request.session.modified = True

    total = subtotal
    return cart_items, subtotal, total


def _money(value):
    return f"{Decimal(value):.2f}"


def _cart_payload(request, **extra):
    """JSON used by the menu page side cart panel and the cart page."""
    cart_items, subtotal, total = _get_cart_totals(request)
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
                'line': i['line'],
                'options_text': i['options_text'],
            }
            for i in cart_items
        ],
        'subtotal': _money(subtotal),
        'discount': '0.00',   # kept so the menu page side panel JS does not break
        'total': _money(total),
        'count': len(cart_items),
        'total_qty': sum(i['quantity'] for i in cart_items),
    }
    data.update(extra)
    return data


def item_options(request, item_id):
    """Option groups of one dish as JSON. The menu popup calls this when ADD is clicked."""
    food_item = get_object_or_404(FoodItem, id=item_id, is_available=True)
    groups = []
    for group in food_item.option_groups.prefetch_related('options'):
        options = [
            {
                'id': o.id,
                'name': o.name,
                'extra_price': _money(o.extra_price),
                'image_url': o.image.url if o.image else '',
                'is_default': o.is_default,
            }
            for o in group.options.all() if o.is_available
        ]
        if not options:
            continue
        groups.append({
            'id': group.id,
            'title': group.title,
            'type': group.group_type,
            'required': group.is_required,
            'max_select': group.max_select,
            'options': options,
        })
    return JsonResponse({'success': True, 'groups': groups})


def cart_data(request):
    """Current cart as JSON. The menu page calls this on load to fill the side panel."""
    return JsonResponse(_cart_payload(request))


def _calc_tip(total, tip_percent):
    """Tip on the total amount. Rounds half-up to 2 places, the same
    way the cart page JavaScript does, so both show the same paisa."""
    if tip_percent not in ALLOWED_TIPS:
        return Decimal('0.00')
    tip = Decimal(total) * Decimal(tip_percent) / Decimal('100')
    return tip.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


ARRIVAL_MODES = ('asap', '15', '30', '45', '60', 'custom')


def _collect_cart_options(data):
    """Reads the choices made on the cart page (order type, arrival, note, tip)
    from a POST dict and returns a clean dict that is safe to keep in the session."""
    order_type = data.get('order_type', 'dine_in')
    if order_type not in ('dine_in', 'takeaway'):
        order_type = 'dine_in'

    arrival_mode = data.get('arrival_mode', 'asap')
    if arrival_mode not in ARRIVAL_MODES:
        arrival_mode = 'asap'

    tip_percent = data.get('tip_percent', '0')
    if tip_percent not in ALLOWED_TIPS:
        tip_percent = '0'

    return {
        'order_type': order_type,
        'arrival_mode': arrival_mode,
        'arrival_custom': data.get('arrival_custom', '').strip()[:5],
        'note': data.get('note', '').strip()[:255],
        'tip_percent': tip_percent,
    }


@require_POST
def save_cart_options(request):
    """Not used any more (there is no customer login). Kept only so the URL in
    orders/urls.py still works. Delete this view and its URL line later."""
    request.session['cart_options'] = _collect_cart_options(request.POST)
    request.session.modified = True
    return JsonResponse({'success': True})


def view_cart(request):
    cart_items, subtotal, total = _get_cart_totals(request)

    # "You may also like": other available dishes that are not in the cart yet
    cart_ids = [i['id'] for i in cart_items]
    suggestions = list(
        FoodItem.objects.orderable().exclude(id__in=cart_ids).order_by('?')[:4]
    )

    # Table from the QR scan (None if the customer did not scan one, or if
    # the table session has ended)
    # CHANGED (guests): the table session is kept too, to decide if the guest box is needed
    selected_table, table_session = _current_table(request)

    ask_guests = _asks_guests(selected_table, table_session)
    available_seats = (
        table_available_seats(selected_table) if selected_table else MAX_GUESTS_NO_TABLE
    )
    table_capacity = available_seats
    known_guests = _known_guests(selected_table, table_session) if selected_table else None
    guest_options = range(1, available_seats + 1)

    context = {
        'cart_items': cart_items,
        'subtotal': subtotal,
        'total': total,
        'suggestions': suggestions,
        'selected_table': selected_table,
        'ask_guests': ask_guests,
        'table_capacity': table_capacity,
        'known_guests': known_guests,
        'available_seats': available_seats,
        'guest_options': guest_options,
        'customer_type_choices': TableSession.CUSTOMER_TYPE_CHOICES,
        'no_seats_available': bool(selected_table and available_seats == 0),
    }
    return render(request, 'orders/cart.html', context)


@require_POST
def update_cart_qty(request, item_id):
    """+ / - buttons on the cart page and the menu side panel. Returns JSON so the page updates live."""
    cart = get_cart(request)
    # 'line' picks one cart line when the same dish was added with different options
    key = request.POST.get('line') or str(item_id)

    if key not in cart:
        return JsonResponse({'success': False, 'error': 'Item not in cart'}, status=404)

    action = request.POST.get('action')
    if action == 'inc':
        cart[key] = min(cart[key] + 1, MAX_QTY)
    elif action == 'dec':
        if cart[key] <= 1 and request.POST.get('remove_at_zero') == '1':
            del cart[key]
        else:
            cart[key] = max(cart[key] - 1, 1)
    else:
        return JsonResponse({'success': False, 'error': 'Bad action'}, status=400)

    save_cart(request, cart)

    cart_items, subtotal, total = _get_cart_totals(request)
    line = next((i for i in cart_items if i['line'] == key), None)

    return JsonResponse(_cart_payload(
        request,
        quantity=cart.get(key, 0),
        line_subtotal=_money(line['subtotal']) if line else '0.00',
    ))


# Coupons are switched off. These two tiny views stay only so the old URLs in
# orders/urls.py still work. You can delete them together with those 2 URL lines later.
@require_POST
def apply_coupon(request):
    messages.info(request, 'Coupons are not available right now.')
    return redirect('orders:view_cart')


@require_POST
def remove_coupon(request):
    request.session.pop('coupon_code', None)
    request.session.modified = True
    return redirect('orders:view_cart')


@require_POST
def remove_from_cart(request, item_id):
    cart = get_cart(request)
    # 'line' picks one cart line when the same dish was added with different options
    key = request.POST.get('line') or str(item_id)

    if key in cart:
        del cart[key]
        save_cart(request, cart)
        if not _is_ajax(request):
            messages.success(request, 'Item removed from cart.')

    if _is_ajax(request):
        return JsonResponse(_cart_payload(request))
    return redirect('orders:view_cart')


def _parse_arrival(request):
    """Reads the arrival choice from the cart form.
    Returns a datetime, or None which means 'as soon as possible'.
    CHANGED (guests-required): the cart page no longer has the arrival section, so
    no arrival_mode is sent and this always returns None (ASAP)."""
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


def confirm_order(request):
    """Creates the order as an UNPAID online order (payment_method 'online',
    payment_status 'pending') and sends the customer to the checkout page.

    PAYMENT FIRST: an unpaid order is invisible to the admin, server and kitchen
    (see Order.objects.visible_to_staff). It reaches them only after
    payment_success marks it paid. If the customer never pays, the order is
    expired after PAYMENT_HOLD_MINUTES.

    No login. The customer must give a name and a 10 digit phone number, and the
    order is remembered in this browser session (guest_order_ids), so only this
    browser can open it (another browser can use Track Order).

    A fresh QR group reserves its selected guest count in its TableSession while
    the table row is locked. Later orders reuse that session's reservation.

    Only a POST from the cart page creates an order. A plain GET (browser refresh,
    back button, link prefetch) never creates anything, it just opens the cart."""
    if request.method != 'POST':
        return redirect('orders:view_cart')

    guest_name = request.POST.get('guest_name', '').strip()[:100]
    guest_phone = _clean_phone(request.POST.get('guest_phone', ''))
    if not guest_name or not guest_phone:
        messages.error(request, 'Please enter your name and a valid 10 digit phone number.')
        return redirect('orders:view_cart')

    cart = get_cart(request)

    if not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:view_cart')

    cart_lines, subtotal, total = _get_cart_totals(request)

    order_items_data = []
    for line in cart_lines:
        order_items_data.append((
            FoodItem.objects.get(id=line['id']),
            line['quantity'],
            line['price'],
            line['options_text'],
        ))

    if not order_items_data:
        messages.error(request, 'No valid items in your cart.')
        return redirect('orders:view_cart')

    # Optional tip (5% or 10% of the total). Always calculated
    # here on the server, the browser only sends the percent.
    tip_percent = request.POST.get('tip_percent', '0')
    total = Decimal(total) + _calc_tip(total, tip_percent)

    # Order options from the cart page form
    order_type = request.POST.get('order_type', 'dine_in')
    if order_type not in ('dine_in', 'takeaway'):
        order_type = 'dine_in'
    arrival_time = _parse_arrival(request)
    note = request.POST.get('note', '').strip()[:255]

    # Customers no longer pick a table in the cart. A table is only attached
    # when they scanned a table QR code, the table session is still active and
    # they are dining in; otherwise the server (or admin) assigns one.
    # Take away orders never get a table.
    #
    # The table comes from the TableSession, not from the URL, so "Order again"
    # from My Orders keeps the same table for the whole visit.
    selected_table = None
    table_session = None
    if order_type == 'dine_in':
        selected_table, table_session = _current_table(request)
        if selected_table and is_table_reserved_now(selected_table.id, request.user):
            messages.warning(request, f'Table #{selected_table.number} is booked now - our staff will assign you a table.')
            selected_table = None
            table_session = None

    qr_session = bool(
        order_type == 'dine_in'
        and table_session is not None
        and table_session.source == TableSession.SOURCE_QR
    )
    guests = None
    validation_error = ''
    customer_type = request.POST.get(
        'customer_type', TableSession.CUSTOMER_TYPE_OTHER
    )
    if qr_session and customer_type not in dict(TableSession.CUSTOMER_TYPE_CHOICES):
        validation_error = 'Choose a valid customer type.'
    if order_type == 'dine_in' and not qr_session:
        needs_guest_count = _asks_guests(selected_table, table_session)
        if not needs_guest_count and table_session is not None:
            guests = table_session.guest_count or table_session.guests
        elif needs_guest_count:
            max_guests = (
                table_available_seats(selected_table)
                if selected_table else MAX_GUESTS_NO_TABLE
            )
            if max_guests < 1:
                validation_error = 'No seats available at this table.'
            raw_guests = request.POST.get('guests', '').strip()
            if not validation_error and raw_guests:
                try:
                    guests = int(raw_guests)
                except ValueError:
                    validation_error = 'Enter a valid number of guests.'
                if not validation_error and (guests < 1 or guests > max_guests):
                    validation_error = (
                        f'Guests must be between 1 and {max_guests}.'
                    )
            elif not validation_error:
                validation_error = 'Please enter how many guests are with you.'

    order = None
    with transaction.atomic():
        if qr_session:
            locked_table = Table.objects.select_for_update().get(pk=selected_table.pk)
            table_session = (
                TableSession.objects.select_for_update()
                .filter(
                    pk=table_session.pk,
                    table=locked_table,
                    is_active=True,
                    closed_at__isnull=True,
                    departed_at__isnull=True,
                )
                .exclude(bills__is_billed=True)
                .exclude(bills__is_closed=True)
                .first()
            )
            if table_session is None:
                validation_error = 'This QR seating session has ended. Please scan the table QR again.'
            elif is_table_reserved_now(locked_table.id, request.user):
                validation_error = f'Table #{locked_table.number} is reserved now.'
            elif table_session.guest_count is not None:
                guests = table_session.guest_count
                selected_table = locked_table
            else:
                raw_guests = request.POST.get('guests', '').strip()
                available = table_available_seats(locked_table)
                try:
                    guests = int(raw_guests)
                except (TypeError, ValueError):
                    guests = None
                    validation_error = 'Please select a valid guest count.'
                if not validation_error and available < 1:
                    validation_error = 'No seats available at this table.'
                elif not validation_error and (guests is None or guests < 1 or guests > available):
                    validation_error = f'Only {available} seat(s) are available at this table.'
                if not validation_error:
                    table_session.guest_count = guests
                    table_session.guests = guests
                    table_session.customer_type = customer_type
                    table_session.save(update_fields=[
                        'guest_count', 'guests', 'customer_type',
                    ])
                    selected_table = locked_table
            if not validation_error:
                Bill.objects.get_or_create(table_session=table_session)

        if not validation_error:
            order = Order.objects.create(
                user=None,
                guest_name=guest_name,
                guest_phone=guest_phone,
                table=selected_table,
                table_session=table_session,
                guests=guests,
                status='pending',
                coupon=None,
                discount_amount=Decimal('0.00'),
                total_amount=total,
                order_type=order_type,
                arrival_time=arrival_time,
                note=note,
                payment_method='online',
                payment_status='pending',
            )

            for food_item, quantity, price, options_text in order_items_data:
                OrderItem.objects.create(
                    order=order,
                    food_item=food_item,
                    quantity=quantity,
                    price=price,
                    options_text=options_text,
                )
            if qr_session and table_session is not None:
                table_session.mark_ordering()

    if validation_error:
        messages.error(request, validation_error)
        return redirect('orders:view_cart')

    # This browser can open the order (payment page, My Orders) from now on
    ids = request.session.get('guest_order_ids', [])
    ids.append(order.id)
    request.session['guest_order_ids'] = ids

    # The table stays in the browser session on purpose (it is NOT removed here),
    # so the next order of the same visit gets the same table. It is removed
    # automatically when the table session ends.
    save_cart(request, {})
    request.session.pop('coupon_code', None)
    request.session.pop('cart_options', None)
    request.session.modified = True

    return redirect('orders:initiate_payment', order_id=order.id)


def _payment_block(request, order):
    """Returns a redirect when this order can no longer be paid, otherwise None.
    Covers: already paid (page refresh / double click), expired, cancelled,
    and an order that waited longer than PAYMENT_HOLD_MINUTES."""
    if order.status != 'pending' or order.payment_status != 'pending':
        if order.payment_status == 'expired':
            messages.error(request, 'Payment time was over, so this order was cancelled. Please order again.')
            return redirect('orders:view_cart')
        return redirect('orders:order_history')

    if order.created_at < timezone.now() - timedelta(minutes=PAYMENT_HOLD_MINUTES):
        order.expire_if_unpaid()
        messages.error(request, 'Payment time was over, so this order was cancelled. Please order again.')
        return redirect('orders:view_cart')

    return None


def initiate_payment(request, order_id):
    """Shows the mock Razorpay checkout page. Only for an order of this browser
    session that is still waiting for payment and is not older than PAYMENT_HOLD_MINUTES."""
    order = get_object_or_404(_my_orders(request), id=order_id)

    blocked = _payment_block(request, order)
    if blocked:
        return blocked

    # Seconds left to pay, counted from when the order was created. This uses the
    # same created_at that _payment_block checks, so the timer and the expiry agree.
    expires_at = order.created_at + timedelta(minutes=PAYMENT_HOLD_MINUTES)
    seconds_left = max(int((expires_at - timezone.now()).total_seconds()), 0)

    return render(request, 'orders/razorpay_checkout.html', {
        'order': order,
        'payment_hold_minutes': PAYMENT_HOLD_MINUTES,
        'seconds_left': seconds_left,
    })


@require_POST
def payment_success(request, order_id):
    """Called when the mock checkout 'completes'. Marks the order PAID. Only now does
    it become visible to the admin / server, who approve it (-> 'confirmed', appears on
    the Kitchen board) or reject it (-> 'cancelled', and a paid order is refunded).

    A customer can ONLY pay online here. Cash orders are created by the server
    (waiter) in the staff dashboard, never from this public page, otherwise anybody
    could post payment_method=cash and push an unpaid order to the kitchen."""
    with transaction.atomic():
        order = get_object_or_404(
            _my_orders(request).select_for_update(), id=order_id
        )

        blocked = _payment_block(request, order)
        if blocked:
            return blocked

        # status is NOT changed here - approval moves it to 'confirmed'
        order.payment_method = 'online'
        order.payment_status = 'paid'

        # This checkout is a MOCK, so no real Razorpay payment exists. A mock id is saved
        # so the automatic refund on cancellation works end to end; mock ids are handled
        # without calling Razorpay.
        # With the REAL Razorpay checkout, verify the signature here (server side) and save
        # the real "pay_xxx" id instead. That real id is what the Razorpay refund API needs.
        order.razorpay_payment_id = f'mock_pay_{order.id}'

        order.save(update_fields=['payment_method', 'payment_status', 'razorpay_payment_id'])

    messages.success(request, f'Payment received. Order #{order.id} is waiting for restaurant approval.')
    return redirect('orders:order_history')


@require_POST
def track_order(request):
    """Open an order on a different browser or phone: order number + phone number.
    On a match the order is added to this browser session, then My Orders opens."""
    raw_id = request.POST.get('order_id', '').strip().lstrip('#')
    phone = _clean_phone(request.POST.get('phone', ''))

    order = None
    if raw_id.isdigit() and phone:
        order = Order.objects.filter(id=int(raw_id), guest_phone=phone).first()

    if order is None:
        messages.error(request, 'No order found for that order number and phone number.')
        return redirect('orders:order_history')

    ids = request.session.get('guest_order_ids', [])
    if order.id not in ids:
        ids.append(order.id)
        request.session['guest_order_ids'] = ids
        request.session.modified = True

    messages.success(request, f'Order #{order.id} is now shown on this device.')
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


def order_history(request):
    _expire_stale_orders(request)

    orders = list(
        _my_orders(request).prefetch_related('items__food_item')
    )

    cancelled_ids = [o.id for o in orders if o.status == 'cancelled']

    # Rejection reasons, saved by the reject view as Notification rows
    reasons = _rejection_reasons(cancelled_ids)

    # Suggestions: other available dishes, preferring the same category
    pool = FoodItem.objects.filter(is_available=True)

    for order in orders:
        order.reject_reason = ''
        order.suggestions = []
        # True while the customer can still open the payment page for this order
        order.can_pay = order.awaiting_payment and order.status == 'pending'
        order.can_customer_cancel = (
            order.source == 'qr'
            and order.payment_method == 'online'
            and order.status in CUSTOMER_CANCELLABLE_STATUSES
        )
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


@require_POST
def cancel_customer_order(request, order_id):
    """Cancel this browser's QR order before preparation starts."""
    choice = (request.POST.get('reason_choice') or '').strip()
    if choice == 'other':
        reason = (request.POST.get('other_reason') or '').strip()
        if not reason:
            return JsonResponse(
                {'ok': False, 'error': 'Enter a reason for choosing Other.'},
                status=400,
            )
    elif choice in CUSTOMER_CANCELLATION_REASONS:
        reason = choice
    else:
        return JsonResponse(
            {'ok': False, 'error': 'Select a valid cancellation reason.'},
            status=400,
        )
    if len(reason) > 200:
        return JsonResponse(
            {'ok': False, 'error': 'Cancellation reason must be 200 characters or fewer.'},
            status=400,
        )

    with transaction.atomic():
        order = get_object_or_404(
            _my_orders(request).select_for_update(),
            pk=order_id,
        )
        if order.source != 'qr' or order.payment_method != 'online':
            return JsonResponse(
                {'ok': False, 'error': 'Only your QR online orders can be cancelled here.'},
                status=409,
            )
        if order.status not in CUSTOMER_CANCELLABLE_STATUSES:
            return JsonResponse(
                {
                    'ok': False,
                    'error': 'This order can no longer be cancelled because preparation has started or it is already closed.',
                },
                status=409,
            )
        if order.payment_status not in ('pending', 'paid'):
            return JsonResponse(
                {'ok': False, 'error': 'This order is not eligible for customer cancellation.'},
                status=409,
            )

        notify_kitchen = order.status == 'confirmed'
        refund_required = order.payment_status == 'paid' and not order.refunded
        order.status = 'cancelled'
        order.cancellation_reason = reason
        order.cancelled_at = timezone.now()
        order.save(update_fields=['status', 'cancellation_reason', 'cancelled_at'])

        if order.coupon_id:
            Coupon.objects.filter(
                pk=order.coupon_id, times_used__gt=0
            ).update(times_used=F('times_used') - 1)

        if notify_kitchen:
            Notification.objects.create(
                kind='cancelled',
                order=order,
                message=f'Order #{order.id} cancelled by customer: {reason}'[:255],
            )

    refund_done = False
    refund_error = ''
    if refund_required:
        refund_done, refund_error = refund_razorpay_payment(order, reason)
        if refund_done:
            order.refunded = True
            order.save(update_fields=['refunded'])

    if refund_required and not refund_done:
        message = 'Your order was cancelled. Your refund is pending restaurant review.'
    elif refund_required:
        message = 'Your order was cancelled and the online payment was refunded.'
    else:
        message = 'Your order was cancelled successfully.'

    return JsonResponse({
        'ok': True,
        'order_id': order.id,
        'reason': order.cancellation_reason,
        'cancelled_at': timezone.localtime(order.cancelled_at).strftime('%d %b %Y, %I:%M %p'),
        'payment_status': order.payment_status,
        'refunded': order.refunded,
        'refund_pending': refund_required and not refund_done,
        'refund_error': refund_error,
        'message': message,
    })


def order_status_poll(request):
    """Light JSON feed used by My Orders to show live status changes."""
    _expire_stale_orders(request)

    orders = list(_my_orders(request).order_by('-created_at')[:60])
    cancelled_ids = [o.id for o in orders if o.status == 'cancelled']
    reasons = _rejection_reasons(cancelled_ids)

    return JsonResponse({
        'orders': [
            {
                'id': o.id,
                'status': o.status,
                'payment_status': o.payment_status,
                'reason': o.cancellation_reason or reasons.get(o.id, ''),
            }
            for o in orders
        ]
    })
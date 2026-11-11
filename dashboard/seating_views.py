"""Walk-in seating for the Server dashboard.

NEW FILE. dashboard/views.py is not changed.

Flow (seats are tracked by booking.models.SeatAllocation):
    check  -> server_walkin_check     read only, suggests tables (seat based)
    seat   -> server_walkin_confirm   creates TableSession + SeatAllocation (occupied)
    cancel -> server_seating_cancel   customer changed their mind before ordering,
                                      seats are freed at once (no cleaning step)
    left   -> server_seating_departed seats move to "cleaning" (still blocked)
    clean  -> server_seating_cleaned  seats are released for the next customer
    order  -> server_take_session_order  server takes an order for a seated group

There is no timer anywhere: seats are freed only by the server's clicks.
"""
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from booking.availability import (
    suggest_walkin_arrangement, table_status_map, upcoming_status_map,
    walkin_blocked_table_ids, walkin_free_seats,
)
from booking.models import SeatAllocation, Table, TableSession
from menu.models import FoodItem
from orders.models import Order, OrderItem
from .decorators import server_required

MAX_GUESTS = 100


def _tables_text(numbers):
    """[10, 12] -> 'Tables 10, 12'"""
    numbers = sorted(numbers)
    label = 'Table' if len(numbers) == 1 else 'Tables'
    return f"{label} " + ', '.join(str(n) for n in numbers)


def _seats_text(seats):
    """[4, 3] -> '4 + 3 = 7 seats'"""
    if len(seats) == 1:
        return f'{seats[0]} seats'
    return ' + '.join(str(s) for s in seats) + f' = {sum(seats)} seats'


def _session_orders(session):
    """Live (not cancelled) orders of this group.
    Order.table_session already exists in orders/models.py, so no migration is needed."""
    return (Order.objects.visible_to_staff()
            .filter(table_session=session)
            .exclude(status='cancelled'))


# ---------------------------------------------------------------------------
# 1. Check (read only)
# ---------------------------------------------------------------------------
@server_required
@require_POST
def server_walkin_check(request):
    """Server types the guests who walked in. Nothing is saved.
    JSON: ok, available, guests, tables [{id, number, seats, capacity}], seats,
    free_seats, warnings, message."""
    try:
        data = json.loads(request.body)
        guests = int(data.get('guests', 0))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Enter a valid number of guests.'}, status=400)

    if guests < 1 or guests > MAX_GUESTS:
        return JsonResponse(
            {'ok': False, 'error': f'Guests must be between 1 and {MAX_GUESTS}.'}, status=400)

    free_seats = sum(free for _, free in walkin_free_seats())
    plan = suggest_walkin_arrangement(guests)   # [(Table, seats_to_give), ...] or None

    warnings = []
    tables = []
    if plan:
        upcoming = upcoming_status_map()
        for table, seats in plan:
            tables.append({
                'id': table.id,
                'number': table.number,
                'seats': seats,
                'capacity': seats,          # the page shows "<capacity> seats" on each chip
                'table_capacity': table.capacity,
            })
            up = upcoming.get(table.id)
            if up:
                when = timezone.localtime(up.start).strftime('%I:%M %p').lstrip('0')
                warnings.append(f'Table {table.number} is booked at {when}.')
        message = (f"{_tables_text([t['number'] for t in tables])} "
                   f"({_seats_text([t['seats'] for t in tables])}) for {guests} guests.")
    else:
        message = f'Only {free_seats} seats are free right now. {guests} guests need more.'

    return JsonResponse({
        'ok': True,
        'available': bool(plan),
        'guests': guests,
        'tables': tables,
        'seats': sum(t['seats'] for t in tables),
        'free_seats': free_seats,
        'warnings': warnings,
        'message': message,
    })


# ---------------------------------------------------------------------------
# 2. Confirm Seating
# ---------------------------------------------------------------------------
@server_required
@require_POST
def server_walkin_confirm(request):
    """Customer accepted the arrangement, server confirms.
    POST JSON: {guests: 7, plan: [{table_id: 10, seats: 4}, {table_id: 12, seats: 3}], group_name: ''}
    The free seats are checked again inside a transaction, so two servers can never
    take the same seats."""
    try:
        data = json.loads(request.body)
        guests = int(data.get('guests', 0))
        plan = [(int(p['table_id']), int(p['seats'])) for p in (data.get('plan') or [])]
        group_name = str(data.get('group_name') or '').strip()[:100]
    except (json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Invalid request.'}, status=400)

    ids = [tid for tid, _ in plan]
    if (guests < 1 or guests > MAX_GUESTS or not plan
            or len(set(ids)) != len(ids)
            or any(seats < 1 for _, seats in plan)
            or sum(seats for _, seats in plan) != guests):
        return JsonResponse({'ok': False, 'error': 'The table plan does not match the guests.'}, status=400)

    with transaction.atomic():
        # Lock the chosen tables, then check the free seats again
        tables = {t.id: t for t in Table.objects.select_for_update().filter(id__in=ids)}
        if len(tables) != len(ids):
            return JsonResponse({'ok': False, 'error': 'A table no longer exists.'}, status=400)

        free = {t.id: f for t, f in walkin_free_seats()}
        for tid, seats in plan:
            if free.get(tid, 0) < seats:
                return JsonResponse({
                    'ok': False,
                    'error': (f'Table {tables[tid].number} has only {free.get(tid, 0)} free seats now. '
                              'Please check again.'),
                }, status=409)

        ordered = sorted(plan, key=lambda p: tables[p[0]].number)
        session = TableSession.objects.create(
            table=tables[ordered[0][0]],          # main table = lowest number
            guests=guests,
            group_name=group_name,
            source=TableSession.SOURCE_WALKIN,
            allocated_by=request.user,
        )
        for tid, seats in ordered:
            SeatAllocation.objects.create(
                session=session, table=tables[tid], seats=seats,
                status=SeatAllocation.OCCUPIED)

    return JsonResponse({
        'ok': True,
        'session_id': session.id,
        'guests': guests,
        'tables': session.table_numbers,
        'seats': session.seats_summary,
    })


# ---------------------------------------------------------------------------
# 3. Seated / cleaning list for the dashboard panel
# ---------------------------------------------------------------------------
@server_required
def server_seating_panel(request):
    """JSON list of groups that block seats right now: seated, or waiting for cleaning."""
    sessions = (TableSession.objects
                .filter(allocations__status__in=[SeatAllocation.OCCUPIED, SeatAllocation.CLEANING])
                .distinct()
                .order_by('started_at'))

    groups = []
    for s in sessions:
        allocs = list(s.allocations
                      .filter(status__in=[SeatAllocation.OCCUPIED, SeatAllocation.CLEANING])
                      .select_related('table')
                      .order_by('table__number'))
        seated = any(a.status == SeatAllocation.OCCUPIED for a in allocs)
        orders = list(_session_orders(s))
        due = sum((o.total_amount for o in orders if o.payment_status != 'paid'), Decimal('0'))
        unserved = sum(1 for o in orders if o.status != 'delivered')
        since = s.started_at if seated else (s.departed_at or s.started_at)
        groups.append({
            'id': s.id,
            'group_name': s.group_name,
            'guests': s.guests,
            'tables': _tables_text([a.table.number for a in allocs]),
            'seats': _seats_text([a.seats for a in allocs]),
            'state': 'seated' if seated else 'cleaning',
            'since': since.isoformat(),
            'order_count': len(orders),
            'due': str(due),
            'unserved': unserved,
            # NEW: the Cancel button is shown only while the group is seated and has no order
            'can_cancel': seated and not orders,
        })
    return JsonResponse({'ok': True, 'groups': groups})


# ---------------------------------------------------------------------------
# 3b. Cancel Seating  (customer changed their mind before ordering)
# ---------------------------------------------------------------------------
@server_required
@require_POST
def server_seating_cancel(request, pk):
    """The group was seated but has not ordered and does not want the seats.
    The seats are freed at once (no cleaning step) and nothing is kept in sales.
    Not allowed once the group has an order. Use Customer Left for that."""
    with transaction.atomic():
        session = get_object_or_404(TableSession.objects.select_for_update(), pk=pk)

        if not session.allocations.filter(status=SeatAllocation.OCCUPIED).exists():
            return JsonResponse({'ok': False, 'error': 'This group is not seated.'}, status=400)

        if _session_orders(session).exists():
            return JsonResponse({
                'ok': False,
                'error': 'This group already has an order. Use Customer Left instead.',
            }, status=400)

        numbers = sorted({a.table.number for a in
                          session.allocations.select_related('table')})

        # Free the seats. Free seats are counted from the SeatAllocation rows.
        session.allocations.all().delete()

        # Remove the empty group too, unless an order (even a cancelled one) points to it
        if not Order.objects.filter(table_session=session).exists():
            session.delete()

    return JsonResponse({
        'ok': True,
        'message': f'{_tables_text(numbers)} are free again.',
    })


# ---------------------------------------------------------------------------
# 4. Customer Left  (seats -> cleaning, still blocked)
# ---------------------------------------------------------------------------
@server_required
@require_POST
def server_seating_departed(request, pk):
    with transaction.atomic():
        session = get_object_or_404(TableSession.objects.select_for_update(), pk=pk)
        if not session.allocations.filter(status=SeatAllocation.OCCUPIED).exists():
            return JsonResponse({'ok': False, 'error': 'This group is not seated.'}, status=400)

        orders = list(_session_orders(session))
        unpaid = [o for o in orders if o.payment_status != 'paid']
        unserved = [o for o in orders if o.status != 'delivered']
        if unpaid:
            return JsonResponse({
                'ok': False,
                'error': f'Payment pending for order #{unpaid[0].id}. Collect Rs.{unpaid[0].total_amount} first.',
            }, status=400)
        if unserved:
            return JsonResponse({'ok': False, 'error': f'Order #{unserved[0].id} is not served yet.'}, status=400)

        session.mark_departed()
        if orders:
            # the group has left: its orders no longer hold a table
            _session_orders(session).update(table_released=True)

    return JsonResponse({'ok': True, 'tables': session.table_numbers, 'message': 'Waiting for cleaning.'})


# ---------------------------------------------------------------------------
# 5. Cleaning Done  (seats are released for the next customer)
# ---------------------------------------------------------------------------
@server_required
@require_POST
def server_seating_cleaned(request, pk):
    with transaction.atomic():
        session = get_object_or_404(TableSession.objects.select_for_update(), pk=pk)
        numbers = sorted({a.table.number for a in
                          session.allocations.filter(status=SeatAllocation.CLEANING).select_related('table')})
        if not numbers:
            return JsonResponse({'ok': False, 'error': 'Nothing is waiting for cleaning.'}, status=400)
        session.confirm_cleaning(request.user)

    return JsonResponse({
        'ok': True,
        'message': f'{_tables_text(numbers)} free for the next customer.',
    })


# ---------------------------------------------------------------------------
# 6. Take Order for a seated group (the order is linked to the group's session)
# ---------------------------------------------------------------------------
@server_required
@require_POST
def server_take_session_order(request):
    """Server takes an order for a seated walk-in group.
    POST JSON: {session_id: 5, items: [{food_item_id: 1, quantity: 2}]}
    Same rules as server_take_order (lands on the Kitchen board, cash at the counter),
    but the order is saved with table_session=<the group>, so it belongs to the group
    and does not close the whole table for other groups."""
    from .views import _deduct_stock_for_order

    try:
        data = json.loads(request.body)
        session_id = int(data.get('session_id'))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Invalid request'}, status=400)

    session = TableSession.objects.select_related('table').filter(pk=session_id).first()
    if not session or not session.allocations.filter(status=SeatAllocation.OCCUPIED).exists():
        return JsonResponse({'ok': False, 'error': 'This group is not seated.'}, status=400)

    rows = data.get('items') or []
    if not rows:
        return JsonResponse({'ok': False, 'error': 'Add at least one item'}, status=400)

    # Collect the lines first so a bad line never leaves a half made order behind
    lines = []
    total = Decimal('0')
    for row in rows:
        try:
            qty = int(row.get('quantity', 0))
            food_id = int(row.get('food_item_id'))
        except (TypeError, ValueError, AttributeError):
            return JsonResponse({'ok': False, 'error': 'Invalid item'}, status=400)
        if qty < 1:
            continue
        food = FoodItem.objects.filter(pk=food_id).first()
        if not food:
            return JsonResponse({'ok': False, 'error': 'A menu item no longer exists'}, status=400)
        if not food.is_available:
            return JsonResponse({'ok': False, 'error': f'{food.name} is not available right now.'}, status=400)
        lines.append((food, qty))
        total += food.price * qty        # price always comes from the database

    if not lines:
        return JsonResponse({'ok': False, 'error': 'Add at least one item'}, status=400)

    with transaction.atomic():
        order = Order.objects.create(
            user=request.user,
            table=session.table,             # main table of the group
            table_session=session,
            order_type='dine_in',
            total_amount=total,
            status='confirmed',              # the Kitchen board reads 'confirmed'
            payment_method='cash',
            payment_status='cash_pending',
            table_released=False,
            source='server',
            taken_by=request.user,
        )
        for food, qty in lines:
            OrderItem.objects.create(order=order, food_item=food, quantity=qty, price=food.price)
        _deduct_stock_for_order(order)

    return JsonResponse({'ok': True, 'order_id': order.id})


# ---------------------------------------------------------------------------
# 7. Table-wise seats for the dashboard: "Table 19: 6 seats, 4 customers, 2 free"
# ---------------------------------------------------------------------------
@server_required
def server_tables_overview(request):
    """One row per table: capacity, customers seated (from SeatAllocation), free seats.
    A table that a whole-table hold blocks (approved booking, or an old style order that
    has no guest count) is shown as 'held' with the reason."""
    rows = walkin_free_seats()
    blocked = walkin_blocked_table_ids()
    holds = table_status_map()

    cleaning = {
        r['table_id']: r['total'] or 0
        for r in (SeatAllocation.objects.filter(status=SeatAllocation.CLEANING)
                  .order_by().values('table_id').annotate(total=Sum('seats')))
    }

    tables = []
    free_total = 0
    for table, free in rows:
        if table.id in blocked:
            hold = holds.get(table.id)
            if hold is not None and hold.kind == 'order':
                note = f'{hold.label}: guest count not entered'
            elif hold is not None:
                note = hold.label
            else:
                note = 'Reserved soon'
            tables.append({
                'id': table.id, 'number': table.number, 'capacity': table.capacity,
                'customers': None, 'free': 0, 'cleaning': 0, 'state': 'held', 'note': note,
            })
            continue

        taken = max(table.capacity - free, 0)
        waiting = min(cleaning.get(table.id, 0), taken)
        customers = taken - waiting
        if free == 0:
            state = 'full'
        elif taken > 0:
            state = 'partial'
        else:
            state = 'free'
        free_total += free
        tables.append({
            'id': table.id, 'number': table.number, 'capacity': table.capacity,
            'customers': customers, 'free': free, 'cleaning': waiting,
            'state': state, 'note': '',
        })

    return JsonResponse({'ok': True, 'tables': tables, 'free_total': free_total})
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

# ---------------------------------------------------------------------------
# Table hold rules
#
# There is NO timer that frees a table. Nothing expires by time.
#   - An approved booking holds its tables from the booked time until the admin
#     clicks "Open Table" (the booking is then marked released).
#   - An order that has a table holds it until the server taps "Customer Left"
#     (table_released), the order is cancelled, or the admin opens the table.
#   - Pending and rejected bookings never hold a table.
#   - EXCEPTION (seat based seating): an order that belongs to a session that
#     has SeatAllocation rows does NOT hold the whole table. Its seats are
#     tracked by SeatAllocation (see booking/seating.py), so other groups can
#     use the remaining seats of the same table.
#   - QR groups reserve seats through their active TableSession.guest_count; their
#     orders never create a whole-table hold.
#   - WALK-IN SEATING also counts guest-counted orders and SeatAllocation rows so
#     the remaining seats can be offered to another group.
#   - NEW: WALK-IN SEATING USES NORMAL TABLES ONLY. A table with table_type='booking'
#     is kept for customer reservations and is never suggested to a walk-in group.
#
# OPEN_ENDED is only a very large number used as the "end" of such a hold so
# the code that compares start/end keeps working. It is never shown.
# ---------------------------------------------------------------------------
OPEN_ENDED = timedelta(days=3650)
HOLD_DURATION = OPEN_ENDED          # kept so old imports do not break; not a real timer

PENDING_HOLD = timedelta(minutes=15)    # unpaid online checkout of an order only
WALKIN_BUFFER = timedelta(minutes=30)   # walk-ins are not seated right before a reservation
STALE_ORDER_LIMIT = timedelta(hours=24) # kept so old imports do not break; no longer used

# Reminder only: after this long a booked table that the admin has not opened
# is shown as "still held". It never frees the table by itself.
OVERDUE_AFTER = timedelta(hours=3)

# If True, a party of 7+ is suggested ONE table when a single free table has enough
# seats (example: a 7 seat Table 5 for 7 guests). Set False to always suggest
# the normal "7+ guests = 2 tables" split.
ALLOW_SINGLE_BIG_TABLE = True


@dataclass
class Hold:
    kind: str            # 'booking' or 'order'
    table_id: int
    start: datetime
    end: datetime
    user_id: int
    label: str
    obj: Any = field(repr=False, default=None)


def booking_start(booking):
    """Aware datetime of the booked slot (date + time are entered in local time)."""
    return timezone.make_aware(datetime.combine(booking.booking_date, booking.booking_time))


def booking_window(booking):
    """(start, end) of a booking hold. The end is open ended: the table stays
    held until the admin opens it."""
    start = booking_start(booking)
    return start, start + OPEN_ENDED


def _is_confirmed_booking(hold):
    """True for an admin-approved booking hold."""
    return hold.kind == 'booking' and getattr(hold.obj, 'status', 'approved') == 'approved'


def _counts_by_seats(hold):
    """NEW: True for an order hold that has a guest count. For walk-in seating such an
    order uses `guests` seats of its table instead of blocking the whole table."""
    return hold.kind == 'order' and bool(getattr(hold.obj, 'guests', None))


def is_overdue(hold):
    """True for a booking hold that started long ago and was never opened by the admin."""
    return hold.kind == 'booking' and timezone.now() - hold.start >= OVERDUE_AFTER


def get_holds():
    """All holds that are still in effect: approved bookings that the admin has not
    opened yet, and live orders. No date or age limit is applied.

    Orders belonging to QR sessions or seat-managed sessions are skipped. QR
    guests and allocated walk-in seats are counted separately from whole-table
    order holds."""
    from orders.models import Order
    from .models import SeatAllocation, TableBooking

    now = timezone.now()
    holds = []

    # Only APPROVED bookings hold tables. A pending request holds nothing
    # (the admin chooses the table(s) when approving), and a rejected one never does.
    # A booking for a big party holds its extra tables too.
    bookings = (
        TableBooking.objects.filter(
            status='approved',
            table__isnull=False,
            released=False,
        )
        .select_related('table', 'user')
        .prefetch_related('extra_tables')
    )
    for b in bookings:
        start, end = booking_window(b)
        label = f"Reserved by {b.name}"
        table_ids = [b.table_id]
        for t in b.extra_tables.all():
            if t.id not in table_ids:
                table_ids.append(t.id)
        for tid in table_ids:
            holds.append(Hold('booking', tid, start, end, b.user_id, label, b))

    # Sessions whose seats are managed seat by seat.
    seat_managed_sessions = set(
        SeatAllocation.objects.values_list('session_id', flat=True).distinct()
    )

    # Orders: a table given to an order (by the customer or by the admin) stays
    # closed until the server taps "Customer Left" (table_released), the order is
    # cancelled, or the admin opens it. A served ('delivered') order still holds the table.
    orders = (
        Order.objects.filter(
            table__isnull=False,
            table_released=False,
        )
        .exclude(status='cancelled')
        .select_related('user', 'table_session')
    )
    for o in orders:
        # The Order field is `table_session`, so the attribute is `table_session_id`.
        if o.table_session_id and o.table_session.source == o.table_session.SOURCE_QR:
            continue
        if o.table_session_id in seat_managed_sessions:
            continue
        unpaid_checkout = (o.status == 'pending' and o.payment_status == 'pending')
        if unpaid_checkout:
            # customer still on the payment step: short hold only
            end = o.created_at + PENDING_HOLD
        else:
            # live order: keep the table closed until the order is finished
            end = now + OPEN_ENDED
        if end > now:
            holds.append(Hold('order', o.table_id, o.created_at, end, o.user_id,
                              f"Order #{o.id} ({o.customer_name})", o))
    return holds


def active_holds(holds=None):
    """Holds that are in effect right now (table is physically occupied)."""
    now = timezone.now()
    holds = get_holds() if holds is None else holds
    return [h for h in holds if h.start <= now < h.end]


def upcoming_holds(holds=None):
    """Holds whose time hasn't started yet - confirmed but not occupying the
    table right now (table is still free to walk in until the start time)."""
    now = timezone.now()
    holds = get_holds() if holds is None else holds
    return [h for h in holds if h.start > now]


def table_status_map(holds=None):
    """{table_id: Hold} - the ACTIVE hold that keeps each table closed right
    now (if several, the one that started last)."""
    result = {}
    for h in active_holds(holds):
        current = result.get(h.table_id)
        if current is None or h.start > current.start:
            result[h.table_id] = h
    return result


def upcoming_status_map(holds=None):
    """{table_id: Hold} - the soonest UPCOMING (not yet started) hold for each
    table, so admin can see a table is reserved for later even though it's
    free right now."""
    result = {}
    for h in upcoming_holds(holds):
        current = result.get(h.table_id)
        if current is None or h.start < current.start:
            result[h.table_id] = h
    return result


def is_table_busy_now(table_id, user=None, buffer=WALKIN_BUFFER):
    """True if someone other than `user` holds the table right now, OR if a
    confirmed booking for this table starts within `buffer` time from now
    (so walk-ins don't get seated right before a reservation arrives).

    Only approved bookings block the table for walk-ins."""
    now = timezone.now()

    # 1. Currently active hold (approved booking time has started, or an order is open)
    for h in active_holds():
        if h.kind == 'booking' and not _is_confirmed_booking(h):
            continue
        if h.table_id == table_id and not (user is not None and h.user_id == user.id):
            return True

    # 2. Upcoming approved reservation starting soon - block walk-ins so it
    #    doesn't clash with the reserved customer arriving.
    for h in upcoming_holds():
        if (
            h.table_id == table_id
            and _is_confirmed_booking(h)
            and h.start - now <= buffer
            and not (user is not None and h.user_id == user.id)
        ):
            return True

    return False


def is_table_reserved_now(table_id, user=None, buffer=WALKIN_BUFFER):
    """True only for an approved booking hold, ignoring existing orders."""
    now = timezone.now()
    for hold in active_holds():
        if (
            hold.kind == 'booking'
            and _is_confirmed_booking(hold)
            and hold.table_id == table_id
            and not (user is not None and hold.user_id == user.id)
        ):
            return True
    for hold in upcoming_holds():
        if (
            hold.table_id == table_id
            and _is_confirmed_booking(hold)
            and hold.start - now <= buffer
            and not (user is not None and hold.user_id == user.id)
        ):
            return True
    return False


def walkin_blocked_table_ids(buffer=WALKIN_BUFFER, holds=None):
    """Set of table ids that CANNOT be used for seat based walk-in seating right now.

    A table is blocked when a whole-table hold applies to it:
      - an approved booking whose time has started and was not opened by the admin,
      - an approved booking that starts within `buffer` from now,
      - an old style live order on the table that has NO guest count.

    CHANGED: an order that has a guest count (Order.guests) no longer blocks the whole
    table. Its guests are subtracted from the free seats in walkin_free_seats().

    This does the same checks as is_table_busy_now() but loads the holds only once,
    so it is cheap to call for the whole restaurant. `holds` can be passed in when
    the caller already loaded them."""
    now = timezone.now()
    holds = get_holds() if holds is None else holds
    blocked = set()

    for h in active_holds(holds):
        if h.kind == 'booking' and not _is_confirmed_booking(h):
            continue
        if _counts_by_seats(h):
            continue            # NEW: counted by seats, not by the whole table
        blocked.add(h.table_id)

    for h in upcoming_holds(holds):
        if _is_confirmed_booking(h) and h.start - now <= buffer:
            blocked.add(h.table_id)

    return blocked


def _order_guest_seats(holds):
    """{table_id: seats} used by non-QR live orders that have a guest count.

    One table can have several orders (the same customers order again: juice,
    dessert). They are the same people, so the table counts the LARGEST guest
    number among its live orders, not the sum."""
    seats = {}
    for h in active_holds(holds):
        if not _counts_by_seats(h):
            continue
        n = int(h.obj.guests)
        seats[h.table_id] = max(seats.get(h.table_id, 0), n)
    return seats


def _legacy_qr_session_seats(table_id=None):
    """Seat counts for pre-token QR sessions whose guest_count is not populated."""
    from django.db.models import Max, Q, Sum
    from orders.models import Order
    from .models import TableSession

    orders = (
        Order.objects.filter(
            table_session__source=TableSession.SOURCE_QR,
            table_session__is_active=True,
            table_session__closed_at__isnull=True,
            table_session__departed_at__isnull=True,
            table_session__guest_count__isnull=True,
            table_released=False,
        )
        .exclude(status='cancelled')
        .filter(
            Q(table_session__bills__isnull=True)
            | Q(table_session__bills__is_billed=False,
                table_session__bills__is_closed=False)
        )
    )
    if table_id is not None:
        orders = orders.filter(table_id=table_id)
    rows = (orders.order_by()
            .values('table_id', 'table_session_id')
            .annotate(guests=Max('guests')))
    seats = {}
    for row in rows:
        seats[row['table_id']] = seats.get(row['table_id'], 0) + (row['guests'] or 0)
    return seats


def table_available_seats(table):
    """Seats available to a new QR group at one table.

    QR groups reserve their guest_count once per active TableSession. Existing
    walk-in seat allocations and legacy live orders continue to count as before.
    """
    from django.db.models import Q, Sum
    from .models import SeatAllocation, TableSession

    now = timezone.now()
    allocations = (
        SeatAllocation.objects
        .filter(
            Q(status__in=[SeatAllocation.OCCUPIED, SeatAllocation.CLEANING])
            | Q(status=SeatAllocation.HELD, held_until__gt=now),
            table=table,
        )
        .aggregate(total=Sum('seats'))['total'] or 0
    )
    qr_sessions = TableSession.objects.filter(
        table=table,
        source=TableSession.SOURCE_QR,
        is_active=True,
        closed_at__isnull=True,
        departed_at__isnull=True,
        guest_count__isnull=False,
    ).filter(
        Q(bills__isnull=True)
        | Q(bills__is_billed=False, bills__is_closed=False)
    )
    qr_guests = qr_sessions.aggregate(total=Sum('guest_count'))['total'] or 0
    legacy_qr_guests = _legacy_qr_session_seats(table.id).get(table.id, 0)
    legacy_guests = _order_guest_seats(get_holds()).get(table.id, 0)
    return max(
        table.capacity - allocations - qr_guests - legacy_qr_guests - legacy_guests,
        0,
    )


# Walk-in Seat Availability (read only helpers, nothing is saved here)
def walkin_free_seats(buffer=WALKIN_BUFFER):
    """[(Table, free_seats), ...] sorted by table number, for seat based walk-in seating.

    NEW: only NORMAL tables (table_type='normal') are listed. Booking Tables are kept
    for customer reservations and never appear here, so they are never suggested to
    a walk-in group.

    free_seats is 0 when a whole-table hold blocks the table (see walkin_blocked_table_ids).
    Otherwise it is:
        capacity
        - seats of live SeatAllocation rows (occupied, awaiting cleaning, or a hold that
          has not expired), the same rule Table.seats_taken uses
        - active QR session guest counts
        - guests of other live orders that have a guest count
    All tables are counted with ONE query for the seats, and the holds are loaded once."""
    from django.db.models import Q, Sum
    from .models import SeatAllocation, Table, TableSession

    now = timezone.now()
    holds = get_holds()
    blocked = walkin_blocked_table_ids(buffer, holds)
    order_seats = _order_guest_seats(holds)

    qr_taken_rows = (
        TableSession.objects
        .filter(
            source=TableSession.SOURCE_QR,
            is_active=True,
            closed_at__isnull=True,
            departed_at__isnull=True,
            guest_count__isnull=False,
        )
        .filter(
            Q(bills__isnull=True)
            | Q(bills__is_billed=False, bills__is_closed=False)
        )
        .order_by()
        .values('table_id')
        .annotate(total=Sum('guest_count'))
    )
    qr_taken = {row['table_id']: row['total'] or 0 for row in qr_taken_rows}
    for table_id, seats in _legacy_qr_session_seats().items():
        qr_taken[table_id] = qr_taken.get(table_id, 0) + seats

    taken_rows = (
        SeatAllocation.objects
        .filter(
            Q(status__in=[SeatAllocation.OCCUPIED, SeatAllocation.CLEANING])
            | Q(status=SeatAllocation.HELD, held_until__gt=now)
        )
        .order_by()                      # no default ordering, so the GROUP BY stays on table_id only
        .values('table_id')
        .annotate(total=Sum('seats'))
    )
    taken = {row['table_id']: row['total'] or 0 for row in taken_rows}

    result = []
    # NEW: Normal tables only (Booking Tables are never used for walk-ins)
    for t in Table.objects.filter(table_type='normal').order_by('number'):
        if t.id in blocked:
            free = 0
        else:
            used = taken.get(t.id, 0) + order_seats.get(t.id, 0) + qr_taken.get(t.id, 0)
            free = max(t.capacity - used, 0)
        result.append((t, free))
    return result


def active_group_counts_by_table():
    """Map table ids to customer groups currently using seats on that table."""
    from django.db.models import Q

    from .models import SeatAllocation, TableSession

    groups = {}
    now = timezone.now()
    for table_id, session_id in (
        SeatAllocation.objects
        .filter(
            Q(status__in=[SeatAllocation.OCCUPIED, SeatAllocation.CLEANING])
            | Q(status=SeatAllocation.HELD, held_until__gt=now)
        )
        .values_list('table_id', 'session_id')
        .distinct()
    ):
        groups.setdefault(table_id, set()).add(session_id)

    qr_sessions = (
        TableSession.objects
        .filter(
            source=TableSession.SOURCE_QR,
            is_active=True,
            closed_at__isnull=True,
            departed_at__isnull=True,
        )
        .filter(
            Q(guest_count__isnull=False)
            | Q(orders__table_id__isnull=False, orders__table_released=False)
        )
        .exclude(status__in=[
            TableSession.STATUS_COMPLETED,
            TableSession.STATUS_CANCELLED,
        ])
        .exclude(bills__is_billed=True)
        .exclude(bills__is_closed=True)
        .distinct()
        .values_list('table_id', 'id')
    )
    for table_id, session_id in qr_sessions:
        groups.setdefault(table_id, set()).add(session_id)

    return {table_id: len(session_ids) for table_id, session_ids in groups.items()}


def suggest_walkin_arrangement(guests, buffer=WALKIN_BUFFER):
    """Suggest where a walk-in group of `guests` can sit.

    Returns [(Table, seats_to_give), ...] sorted by table number, or None if the
    restaurant does not have enough free seats right now.

    Only Normal tables are used (see walkin_free_seats).

    1. If ONE table has enough free seats, use the smallest such table
       (so big tables stay free for big groups).
    2. Otherwise split the group over the FEWEST tables, taking the tables with
       the most free seats first (example: 7 guests -> Table 10: 4 seats + Table 12: 3 seats).

    Nothing is saved. The caller creates the TableSession and SeatAllocation rows
    only after the customer accepts and the server confirms."""
    if guests < 1:
        return None

    rows = walkin_free_seats(buffer)
    if sum(free for _, free in rows) < guests:
        return None

    singles = sorted((r for r in rows if r[1] >= guests), key=lambda r: (r[1], r[0].number))
    if singles:
        return [(singles[0][0], guests)]

    ordered = sorted((r for r in rows if r[1] > 0), key=lambda r: (-r[1], r[0].number))
    remaining = guests
    picked = []
    for table, free in ordered:
        if remaining <= 0:
            break
        take = min(free, remaining)
        picked.append((table, take))
        remaining -= take

    picked.sort(key=lambda p: p[0].number)
    return picked if remaining <= 0 else None


def _same_local_date(a, b):
    return timezone.localtime(a).date() == timezone.localtime(b).date()


def slot_conflicts(start, holds=None, exclude_booking_id=None):
    """{table_id: Hold} for tables that are still held, on the same day, by a
    booking or order that started at or before `start` and has not been opened
    by the admin.

    There is no timer, so the system cannot know when a party will leave.
    The admin screen uses this only to show a warning like
    "Held by booking #128 (2:00 PM)". The admin can still pick such a table.
    `exclude_booking_id` skips the booking that is being edited."""
    holds = get_holds() if holds is None else holds
    result = {}
    for h in holds:
        if exclude_booking_id is not None and h.kind == 'booking' and h.obj.pk == exclude_booking_id:
            continue
        if h.start <= start and _same_local_date(h.start, start):
            current = result.get(h.table_id)
            if current is None or h.start > current.start:
                result[h.table_id] = h
    return result


def later_reservations(start, holds=None, exclude_booking_id=None):
    """{table_id: Hold} for tables that an APPROVED booking has reserved LATER on the
    same day (it starts after `start`).

    Why this matters: a booking at `start` keeps its tables until the admin opens them.
    If a table is also reserved for a later approved booking, the two bookings clash
    (example: 4:00 PM and 4:50 PM both on Table 1). slot_conflicts() only looks at
    earlier holds, so this function covers the other direction.
    If several, the soonest later booking is returned."""
    holds = get_holds() if holds is None else holds
    result = {}
    for h in holds:
        if h.kind != 'booking' or not _is_confirmed_booking(h):
            continue
        if exclude_booking_id is not None and h.obj.pk == exclude_booking_id:
            continue
        if h.start > start and _same_local_date(h.start, start):
            current = result.get(h.table_id)
            if current is None or h.start < current.start:
                result[h.table_id] = h
    return result


def table_conflicts(start, table_ids, exclude_booking_id=None):
    """Clash check for the admin approve screen. For the chosen `table_ids` returns a
    list of dicts, sorted by table number:

        {'table_id': 1, 'when': 'before', 'hold': <Hold>}   still held by an earlier booking/order
        {'table_id': 1, 'when': 'after',  'hold': <Hold>}   reserved by a later approved booking

    An empty list means no clash. The approve view should refuse (and show these
    messages) unless the admin sends force=1."""
    holds = get_holds()
    earlier = slot_conflicts(start, holds, exclude_booking_id)
    later = later_reservations(start, holds, exclude_booking_id)

    out = []
    for tid in table_ids:
        if tid in earlier:
            out.append({'table_id': tid, 'when': 'before', 'hold': earlier[tid]})
        if tid in later:
            out.append({'table_id': tid, 'when': 'after', 'hold': later[tid]})
    return sorted(out, key=lambda c: c['table_id'])


def describe_conflict(conflict, table_label=None):
    """One readable line for a table_conflicts() item.
    Example: 'Table 1 is reserved for a later booking: Reserved by balachandrun (4:50 PM)'."""
    hold = conflict['hold']
    when_text = timezone.localtime(hold.start).strftime('%I:%M %p').lstrip('0')
    name = table_label or f"Table id {conflict['table_id']}"
    if conflict['when'] == 'before':
        return f"{name} is still held: {hold.label} ({when_text})"
    return f"{name} is reserved for a later booking: {hold.label} ({when_text})"


def busy_table_ids_for_slot(start, holds=None, exclude_booking_id=None):
    """Tables that are still held when a new booking at `start` is planned."""
    return set(slot_conflicts(start, holds, exclude_booking_id))


def free_tables_for_slot(start, guests=1, exclude_booking_id=None):
    """Free tables (big enough for `guests`) for that slot, smallest first."""
    from .models import Table

    busy = busy_table_ids_for_slot(start, exclude_booking_id=exclude_booking_id)
    return [
        t for t in Table.objects.filter(capacity__gte=guests).order_by('capacity', 'number')
        if t.id not in busy
    ]


def pick_tables_for_party(start, guests, preferred=None, exclude_booking_id=None):
    """Suggest tables for a party at this slot. The admin approve screen uses this only
    as the pre-selected suggestion; the admin makes the final choice.

    Tables that are free but already reserved for a LATER approved booking the same day
    are only used when nothing else is left (so the suggestion does not create a
    double booking like 4:00 PM and 4:50 PM on Table 1).

    Up to 6 guests: one free table that is big enough (the preferred one if it is free).
    7+ guests: one single free table if it has enough seats (ALLOW_SINGLE_BIG_TABLE),
    otherwise as many free tables as needed (7-12 = 2, 13-18 = 3 ... 30 = 5).
    Returns a list of Table objects (main table first), or None if there are not enough free tables."""
    from .models import Table
    from .forms import SEATS_PER_TABLE, tables_needed

    busy = busy_table_ids_for_slot(start, exclude_booking_id=exclude_booking_id)
    later = set(later_reservations(start, exclude_booking_id=exclude_booking_id))

    def prefer(t):
        # tables without a later reservation first, then smallest, then by number
        return (t.id in later, t.capacity, t.number)

    if guests <= SEATS_PER_TABLE:
        free = sorted(free_tables_for_slot(start, guests, exclude_booking_id=exclude_booking_id),
                      key=prefer)
        if (preferred is not None and preferred.capacity >= guests
                and preferred.id not in busy and preferred.id not in later):
            return [preferred]
        return free[:1] or None

    free_all = [t for t in Table.objects.all() if t.id not in busy]

    # One table is enough when it really seats the whole party (example: 7 seat table, 7 guests)
    if ALLOW_SINGLE_BIG_TABLE:
        single = sorted((t for t in free_all if t.capacity >= guests), key=prefer)
        if single:
            return [single[0]]

    needed = tables_needed(guests)
    # Prefer tables that really seat a full group of 6, smaller tables only if we run short
    full = sorted((t for t in free_all if t.capacity >= SEATS_PER_TABLE), key=prefer)
    small = sorted((t for t in free_all if t.capacity < SEATS_PER_TABLE), key=prefer)
    chosen = []
    if preferred is not None and preferred.id not in busy:
        chosen.append(preferred)
    for t in full + small:
        if len(chosen) >= needed:
            break
        if t.id not in {c.id for c in chosen}:
            chosen.append(t)
    return chosen if len(chosen) >= needed else None


def release_table(table_id):
    """Admin 'Open Table': lift every hold that is active on this table right now.
    Returns how many holds were lifted.
    For a multi-table booking this opens the whole booking (all its tables).

    get_holds() has no date limit any more, so every approved booking that was
    never opened (however old) is found here and can be opened.

    Seat based groups are not affected: their seats are released through
    TableSession.mark_departed() and TableSession.confirm_cleaning()."""
    now = timezone.now()
    count = 0
    for h in active_holds():
        if h.table_id != table_id:
            continue
        if h.kind == 'booking':
            h.obj.released = True
            h.obj.released_at = now
            h.obj.save(update_fields=['released', 'released_at'])
        else:
            h.obj.table_released = True
            h.obj.save(update_fields=['table_released'])
        count += 1
    return count
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

HOLD_DURATION = timedelta(hours=2)
PENDING_HOLD = timedelta(minutes=15)
WALKIN_BUFFER = timedelta(minutes=30)
STALE_ORDER_LIMIT = timedelta(hours=24)


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
    start = booking_start(booking)
    return start, start + HOLD_DURATION


def _is_confirmed_booking(hold):
    """True for an admin-approved booking hold."""
    return hold.kind == 'booking' and getattr(hold.obj, 'status', 'approved') == 'approved'


def get_holds():
    """All holds that have not expired yet and were not opened by the admin."""
    from orders.models import Order
    from .models import TableBooking

    now = timezone.now()
    holds = []

    # Rejected bookings never hold a table.
    # Pending bookings hold the slot (so two customers cannot request the same table),
    # but they are labelled and are NOT treated as confirmed (see is_table_busy_now).
    # CHANGED: a booking for a big party also holds its extra tables.
    bookings = (
        TableBooking.objects.filter(
            table__isnull=False,
            released=False,
            booking_date__gte=timezone.localdate() - timedelta(days=1),
        )
        .exclude(status='rejected')
        .select_related('table', 'user')
        .prefetch_related('extra_tables')
    )
    for b in bookings:
        start, end = booking_window(b)
        if end > now:
            label = f"Reserved by {b.name}"
            if b.status == 'pending':
                label = f"Waiting for approval: {b.name}"
            table_ids = [b.table_id]
            for t in b.extra_tables.all():
                if t.id not in table_ids:
                    table_ids.append(t.id)
            for tid in table_ids:
                holds.append(Hold('booking', tid, start, end, b.user_id, label, b))

    # Orders: a table given to an order (by the customer or by the admin) stays
    # closed until the order is delivered / cancelled / opened by the admin.
    orders = (
        Order.objects.filter(
            table__isnull=False,
            table_released=False,
            created_at__gte=now - STALE_ORDER_LIMIT,
        )
        .exclude(status__in=['cancelled', 'delivered'])
        .select_related('user')
    )
    for o in orders:
        unpaid_checkout = (o.status == 'pending' and o.payment_status == 'pending')
        if unpaid_checkout:
            # customer still on the payment step: short hold only
            end = o.created_at + PENDING_HOLD
        else:
            # live order: keep the table closed until the order is finished
            end = now + HOLD_DURATION
        if end > now:
            holds.append(Hold('order', o.table_id, o.created_at, end, o.user_id,
                              f"Order #{o.id} ({o.user.username})", o))
    return holds


def active_holds(holds=None):
    """Holds that are in effect right now (table is physically occupied)."""
    now = timezone.now()
    holds = get_holds() if holds is None else holds
    return [h for h in holds if h.start <= now < h.end]


def upcoming_holds(holds=None):
    """Holds whose window hasn't started yet - confirmed but not occupying the
    table right now (table is still free to walk in until the start time)."""
    now = timezone.now()
    holds = get_holds() if holds is None else holds
    return [h for h in holds if h.start > now]


def table_status_map(holds=None):
    """{table_id: Hold} - the ACTIVE hold that keeps each table closed right
    now (if several, the one that ends last)."""
    result = {}
    for h in active_holds(holds):
        current = result.get(h.table_id)
        if current is None or h.end > current.end:
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

    A booking that is still waiting for admin approval does not
    block the table for walk-ins. Only approved bookings do."""
    now = timezone.now()

    # 1. Currently active hold (approved booking window has started, or an order is open)
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


def busy_table_ids_for_slot(start, holds=None):
    """Tables that would clash with a new 2-hour booking starting at `start`."""
    end = start + HOLD_DURATION
    holds = get_holds() if holds is None else holds
    return {h.table_id for h in holds if h.start < end and start < h.end}


def free_tables_for_slot(start, guests=1):
    """Free tables (big enough for `guests`) for that slot, smallest first."""
    from .models import Table

    busy = busy_table_ids_for_slot(start)
    return [
        t for t in Table.objects.filter(capacity__gte=guests).order_by('capacity', 'number')
        if t.id not in busy
    ]


def pick_tables_for_party(start, guests, preferred=None):
    """NEW: tables for a party at this slot. One table seats 6 guests.

    Up to 6 guests: one free table that is big enough (the preferred one if it is free).
    7+ guests: as many free tables as needed (7-12 = 2, 13-18 = 3 ... 30 = 5).
    Returns a list of Table objects (main table first), or None if there are not enough free tables."""
    from .models import Table
    from .forms import SEATS_PER_TABLE, tables_needed

    busy = busy_table_ids_for_slot(start)

    if guests <= SEATS_PER_TABLE:
        free = free_tables_for_slot(start, guests)
        if preferred is not None and preferred.capacity >= guests and preferred.id not in busy:
            return [preferred]
        return free[:1] or None

    needed = tables_needed(guests)
    free = [t for t in Table.objects.all().order_by('capacity', 'number') if t.id not in busy]
    # Prefer tables that really seat a full group of 6, smaller tables only if we run short
    full = [t for t in free if t.capacity >= SEATS_PER_TABLE]
    small = [t for t in free if t.capacity < SEATS_PER_TABLE]
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
    For a multi-table booking this opens the whole booking (all its tables)."""
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
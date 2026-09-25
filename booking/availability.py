from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

HOLD_DURATION = timedelta(hours=2)
PENDING_HOLD = timedelta(minutes=15)
WALKIN_BUFFER = timedelta(minutes=30)


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


def get_holds():
    """All holds that have not expired yet and were not opened by the admin."""
    from orders.models import Order
    from .models import TableBooking

    now = timezone.now()
    holds = []

    bookings = TableBooking.objects.filter(
        table__isnull=False,
        released=False,
        booking_date__gte=timezone.localdate() - timedelta(days=1),
    ).select_related('table', 'user')
    for b in bookings:
        start, end = booking_window(b)
        if end > now:
            holds.append(Hold('booking', b.table_id, start, end, b.user_id,
                              f"Reserved by {b.name}", b))

    orders = (
        Order.objects.filter(
            table__isnull=False,
            table_released=False,
            created_at__gte=now - HOLD_DURATION,
        )
        .exclude(status='cancelled')
        .select_related('user')
    )
    for o in orders:
        end = o.created_at + (PENDING_HOLD if o.status == 'pending' else HOLD_DURATION)
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
    (so walk-ins don't get seated right before a reservation arrives)."""
    now = timezone.now()

    # 1. Currently active hold (booking window has started, or an order is open)
    for h in active_holds():
        if h.table_id == table_id and not (user is not None and h.user_id == user.id):
            return True

    # 2. Upcoming reservation starting soon - block walk-ins so it doesn't
    #    clash with the reserved customer arriving.
    for h in upcoming_holds():
        if (
            h.table_id == table_id
            and h.kind == 'booking'
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


def release_table(table_id):
    """Admin 'Open Table': lift every hold that is active on this table right now.
    Returns how many holds were lifted."""
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
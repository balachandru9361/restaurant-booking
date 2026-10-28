from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

# ---------------------------------------------------------------------------
# Table hold rules
#
# There is NO timer that frees a booked table. An approved booking holds its
# tables from the booked time until the admin clicks "Open Table" (the booking
# is then marked released). Pending and rejected bookings never hold a table.
#
# OPEN_ENDED is only a very large number used as the "end" of such a hold so
# the old code that compares start/end keeps working. It is never shown.
# ---------------------------------------------------------------------------
OPEN_ENDED = timedelta(days=3650)
HOLD_DURATION = OPEN_ENDED          # kept so old imports do not break; not a real timer

PENDING_HOLD = timedelta(minutes=15)    # unpaid online checkout of an order only
WALKIN_BUFFER = timedelta(minutes=30)
STALE_ORDER_LIMIT = timedelta(hours=24)

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


def is_overdue(hold):
    """True for a booking hold that started long ago and was never opened by the admin."""
    return hold.kind == 'booking' and timezone.now() - hold.start >= OVERDUE_AFTER


def get_holds():
    """All holds that are still in effect: approved bookings that the admin has not
    opened yet, and live orders."""
    from orders.models import Order
    from .models import TableBooking

    now = timezone.now()
    holds = []

    # Only APPROVED bookings hold tables. A pending request holds nothing
    # (the admin chooses the table(s) when approving), and a rejected one never does.
    # A booking for a big party holds its extra tables too.
    # The date filter looks back one day only: it is a safety guard so old approved
    # bookings that were never opened (from before this change) do not block tables forever.
    bookings = (
        TableBooking.objects.filter(
            status='approved',
            table__isnull=False,
            released=False,
            booking_date__gte=timezone.localdate() - timedelta(days=1),
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

    # Orders: a table given to an order (by the customer or by the admin) stays
    # closed until the server taps "Customer Left" (table_released), the order is
    # cancelled, or the admin opens it. A served ('delivered') order still holds the table.
    orders = (
        Order.objects.filter(
            table__isnull=False,
            table_released=False,
            created_at__gte=now - STALE_ORDER_LIMIT,
        )
        .exclude(status='cancelled')
        .select_related('user')
    )
    for o in orders:
        unpaid_checkout = (o.status == 'pending' and o.payment_status == 'pending')
        if unpaid_checkout:
            # customer still on the payment step: short hold only
            end = o.created_at + PENDING_HOLD
        else:
            # live order: keep the table closed until the order is finished
            end = now + OPEN_ENDED
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

    CHANGED: get_holds() only looks back one day, so an older approved booking that
    was never opened (example: booked 3 days ago, shown as "Still held - time passed")
    was not found and Open Table did nothing. Those old bookings are opened here too."""
    from django.db.models import Q
    from .models import TableBooking

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

    # Old approved bookings (older than the get_holds look-back day) that were never opened
    cutoff = timezone.localdate() - timedelta(days=1)
    stale = (TableBooking.objects
             .filter(Q(table_id=table_id) | Q(extra_tables__id=table_id),
                     status='approved', released=False, booking_date__lt=cutoff)
             .distinct())
    for b in stale:
        b.released = True
        b.released_at = now
        b.save(update_fields=['released', 'released_at'])
        count += 1
    return count
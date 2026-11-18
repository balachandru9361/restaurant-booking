from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction
from django.db.models import Q, Sum
from django.contrib.auth.models import User
from django.utils import timezone


class Table(models.Model):
    # Table types: normal tables are used for walk-in guests,
    # booking tables are used only for customer reservations.
    NORMAL = 'normal'
    BOOKING = 'booking'
    TYPE_CHOICES = [
        (NORMAL, 'Normal Table'),
        (BOOKING, 'Booking Table'),
    ]

    number = models.PositiveIntegerField(unique=True)
    capacity = models.PositiveIntegerField()
    # Existing tables get 'normal' automatically through the default.
    table_type = models.CharField(
        max_length=10,
        choices=TYPE_CHOICES,
        default=NORMAL,
        db_index=True,
    )

    @property
    def is_booking_table(self):
        return self.table_type == self.BOOKING

    @property
    def is_normal_table(self):
        return self.table_type == self.NORMAL

    # ------------------------------------------------------------------
    # Live seat availability (read helpers).
    # Seats are counted from SeatAllocation rows, never stored on the table,
    # so there is only one source of truth.
    # NOTE: these do a query per call. For many tables at once, use the
    # central seating service (annotate / prefetch) instead of looping.
    # ------------------------------------------------------------------
    def _live_allocations(self):
        """Allocations that currently block seats: occupied, awaiting
        cleaning, or a temporary hold that has not expired yet."""
        now = timezone.now()
        return self.allocations.filter(
            Q(status__in=[SeatAllocation.OCCUPIED, SeatAllocation.CLEANING])
            | Q(status=SeatAllocation.HELD, held_until__gt=now)
        )

    @property
    def seats_taken(self):
        """Seats that cannot be given to a new group right now."""
        return self._live_allocations().aggregate(total=Sum('seats'))['total'] or 0

    @property
    def seats_free(self):
        return max(self.capacity - self.seats_taken, 0)

    @property
    def seats_cleaning(self):
        """Seats of groups that left and are waiting for the cleaning confirmation."""
        return (
            self.allocations.filter(status=SeatAllocation.CLEANING)
            .aggregate(total=Sum('seats'))['total'] or 0
        )

    @property
    def seat_status(self):
        """'available' / 'partial' / 'full' / 'cleaning' for dashboards."""
        taken = self.seats_taken
        if taken == 0:
            return 'available'
        if self.seats_cleaning and self.seats_cleaning == taken:
            return 'cleaning'
        if taken >= self.capacity:
            return 'full'
        return 'partial'

    def __str__(self):
        return f"Table {self.number} (seats {self.capacity})"


class TableSession(models.Model):
    """One dine-in visit of ONE customer group.

    Several groups may sit at the same table (each with its own session), so a
    table can have many active sessions. Orders (and reorders) are allowed only
    while is_active is True. The seats a group uses live in SeatAllocation.
    A group may span several tables; `table` is its main table."""

    SOURCE_QR = 'qr'
    SOURCE_WALKIN = 'walkin'
    SOURCE_CHOICES = [
        (SOURCE_QR, 'QR scan'),
        (SOURCE_WALKIN, 'Walk-in (server)'),
    ]

    table = models.ForeignKey(Table, on_delete=models.PROTECT, related_name='sessions')
    started_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Customer group details.
    group_name = models.CharField(max_length=100, blank=True)
    guests = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_QR)
    allocated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='allocated_sessions',
    )
    departed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']

    @classmethod
    def get_or_start(cls, table):
        """Used on QR scan: reuse the latest active session of the table or
        start a new one. If a table has several active groups, the QR view
        must pick the correct group instead of relying on this method."""
        session = (
            cls.objects.filter(table=table, is_active=True)
            .order_by('-started_at')
            .first()
        )
        if session:
            return session
        return cls.objects.create(table=table, source=cls.SOURCE_QR)

    def close(self):
        """Call this when the bill is paid. This stops new orders only.
        It does NOT release the seats: seats are released only after the
        group is marked departed and the cleaning is confirmed."""
        self.is_active = False
        self.closed_at = timezone.now()
        self.save(update_fields=['is_active', 'closed_at'])

    # ------------------------------------------------------------------
    # Seat lifecycle of this group.
    # ------------------------------------------------------------------
    @transaction.atomic
    def occupy(self):
        """Temporary hold -> seated. Clears the hold expiry."""
        self.allocations.filter(status=SeatAllocation.HELD).update(
            status=SeatAllocation.OCCUPIED, held_until=None
        )

    @transaction.atomic
    def cancel_hold(self):
        """Customer declined or cancelled while the seats were only held."""
        self.allocations.filter(status=SeatAllocation.HELD).update(
            status=SeatAllocation.CANCELLED, held_until=None
        )
        if self.is_active and not self.allocations.exclude(
            status=SeatAllocation.CANCELLED
        ).exists():
            self.close()

    @transaction.atomic
    def mark_departed(self):
        """Group finished dining. Seats move to 'awaiting cleaning' (still
        blocked). Orders are stopped if the session is still active."""
        now = timezone.now()
        self.allocations.filter(status=SeatAllocation.OCCUPIED).update(
            status=SeatAllocation.CLEANING, departed_at=now
        )
        self.departed_at = now
        fields = ['departed_at']
        if self.is_active:
            self.is_active = False
            self.closed_at = now
            fields += ['is_active', 'closed_at']
        self.save(update_fields=fields)

    @transaction.atomic
    def confirm_cleaning(self, user=None):
        """Server confirms the table area is clean. Seats become available."""
        self.allocations.filter(status=SeatAllocation.CLEANING).update(
            status=SeatAllocation.RELEASED,
            cleaned_at=timezone.now(),
            cleaned_by=user,
        )

    @property
    def live_allocations(self):
        """Allocations that still block seats (held, occupied, cleaning)."""
        return self.allocations.filter(
            status__in=[
                SeatAllocation.HELD,
                SeatAllocation.OCCUPIED,
                SeatAllocation.CLEANING,
            ]
        ).select_related('table').order_by('table__number')

    @property
    def table_numbers(self):
        """Display text such as 'Tables 10, 12' for this group."""
        numbers = sorted({a.table.number for a in self.live_allocations})
        if not numbers:
            numbers = [self.table.number]
        label = 'Table' if len(numbers) == 1 else 'Tables'
        return f"{label} " + ", ".join(str(n) for n in numbers)

    @property
    def seats_summary(self):
        """'4 + 3 = 7 seats' style text for dashboards."""
        seats = [a.seats for a in self.live_allocations]
        if not seats:
            return ''
        if len(seats) == 1:
            return f"{seats[0]} seats"
        return " + ".join(str(s) for s in seats) + f" = {sum(seats)} seats"

    def __str__(self):
        state = 'active' if self.is_active else 'closed'
        return f"Table {self.table.number} session ({state})"


class SeatAllocation(models.Model):
    """Seats given to one customer group (session) at one table.
    A group of 7 on Table 10 + Table 12 has two rows: 4 seats and 3 seats.

    Lifecycle: held (optional, temporary) -> occupied -> cleaning -> released.
    A declined or expired hold becomes 'cancelled'."""

    HELD = 'held'
    OCCUPIED = 'occupied'
    CLEANING = 'cleaning'
    RELEASED = 'released'
    CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (HELD, 'Temporarily held'),
        (OCCUPIED, 'Occupied'),
        (CLEANING, 'Awaiting cleaning'),
        (RELEASED, 'Released'),
        (CANCELLED, 'Cancelled'),
    ]

    session = models.ForeignKey(TableSession, on_delete=models.CASCADE, related_name='allocations')
    table = models.ForeignKey(Table, on_delete=models.PROTECT, related_name='allocations')
    seats = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OCCUPIED, db_index=True)

    held_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    departed_at = models.DateTimeField(null=True, blank=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)
    cleaned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cleaned_allocations',
    )

    class Meta:
        ordering = ['table__number', 'id']
        indexes = [models.Index(fields=['table', 'status'])]
        constraints = [
            # CHANGED: Django 5.1+ uses condition= (check= was removed in Django 6.0)
            models.CheckConstraint(condition=Q(seats__gte=1), name='allocation_seats_positive'),
        ]

    def __str__(self):
        return f"Table {self.table.number}: {self.seats} seats ({self.status})"


class TableBooking(models.Model):
    OCCASION_CHOICES = [
        ('none', 'No specific occasion'),
        ('birthday', 'Birthday'),
        ('anniversary', 'Anniversary'),
        ('family', 'Family gathering'),
        ('business', 'Business meeting'),
        ('date', 'Date night'),
        ('other', 'Other'),
    ]

    # A booking waits for the admin before it is confirmed.
    # 'cancelled' means the customer cancelled it; the record is kept.
    STATUS_CHOICES = [
        ('pending', 'Waiting for admin approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='table_bookings')

    # Tables are chosen by the admin when the booking is approved.
    # A new customer request has no table (table is empty) until then.
    # `table` is the main table, `extra_tables` are the additional tables
    # for bigger parties.
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    extra_tables = models.ManyToManyField(Table, blank=True, related_name='extra_bookings')

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    # Up to 30 guests.
    guests = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(30)],
    )

    booking_date = models.DateField()
    booking_time = models.TimeField()

    occasion = models.CharField(max_length=20, choices=OCCASION_CHOICES, default='none')
    special_request = models.TextField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # Admin approval
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    reject_reason = models.CharField(max_length=200, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    # The admin clicks "Open Table" to free the booked tables.
    # Tables are never freed automatically by a timer.
    released = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-booking_date', '-booking_time']

    @property
    def all_tables(self):
        """Every table of this booking (main + extra), sorted by table number.
        Customer pass, server dashboard and admin pages must all read tables from here
        so they always show the same thing. Works only after the booking is saved."""
        tables = []
        if self.table_id:
            tables.append(self.table)
        if self.pk:
            tables.extend(t for t in self.extra_tables.all() if t.id != self.table_id)
        return sorted(tables, key=lambda t: t.number)

    @property
    def has_tables(self):
        """True once the admin has chosen at least one table."""
        return len(self.all_tables) > 0

    @property
    def table_numbers(self):
        """Table numbers in order, for display. Example: 'Tables 1, 2, 5'."""
        numbers = [t.number for t in self.all_tables]
        if not numbers:
            return ''
        label = 'Table' if len(numbers) == 1 else 'Tables'
        return f"{label} " + ", ".join(str(n) for n in numbers)

    @property
    def total_seats(self):
        """Total seats of all tables chosen for this booking."""
        return sum(t.capacity for t in self.all_tables)

    @property
    def seats_summary(self):
        """Real seat text for the customer pass (replaces the hardcoded '6 seats per table').
        '6 seats' / '6 seats per table' / '6 + 6 + 7 = 19 seats'."""
        caps = [t.capacity for t in self.all_tables]
        if not caps:
            return ''
        if len(caps) == 1:
            return f"{caps[0]} seats"
        if len(set(caps)) == 1:
            return f"{caps[0]} seats per table"
        return " + ".join(str(c) for c in caps) + f" = {sum(caps)} seats"

    def __str__(self):
        return f"{self.name} - {self.booking_date} {self.booking_time}"
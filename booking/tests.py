from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from orders.views import _get_tables_with_status
from .availability import (
    free_tables_for_slot, is_table_busy_now, release_table, table_status_map,
)
from .models import Table, TableBooking


# Tests use a day in the near future so the one-day look-back guard never hides a booking.
TEST_DAY = timezone.localdate() + timedelta(days=2)


def at(hour, minute=0):
    """Aware datetime on TEST_DAY (2 days from today) at hour:minute, local time."""
    return timezone.make_aware(datetime(TEST_DAY.year, TEST_DAY.month, TEST_DAY.day, hour, minute))


class TableHoldTests(TestCase):
    """Rules: there is NO timer. An approved booking holds its table(s) from the booked time
    until the admin clicks "Open Table". Pending and rejected bookings hold nothing.
    7 or more guests need 2 tables (unless one table has enough seats for the whole party).
    The admin cannot approve a table that clashes with an earlier hold or a later approved booking
    (unless force=1)."""

    def setUp(self):
        self.t4 = Table.objects.create(number=1, capacity=4)
        self.t6 = Table.objects.create(number=2, capacity=6)
        self.alice = User.objects.create_user('alice', password='x')
        self.bob = User.objects.create_user('bob', password='x')

    def book(self, user, when, guests=3, table=None, status='approved', extra=()):
        booking = TableBooking.objects.create(
            user=user, name=user.username, phone='9876543210', guests=guests,
            booking_date=when.date(), booking_time=when.time(), table=table,
            status=status,
        )
        if extra:
            booking.extra_tables.set(extra)
        return booking

    def admin_client(self):
        admin = User.objects.create_superuser('admin', password='x')
        self.client.force_login(admin)

    def post_booking(self, guests, time='19:00'):
        return self.client.post(reverse('book_table'), {
            'name': 'Alice', 'phone': '9876543210', 'guests': guests,
            'booking_date': TEST_DAY.isoformat(), 'booking_time': time,
            'occasion': 'none', 'special_request': '',
        })

    # ---- customer booking flow ----
    def test_customer_booking_is_pending_with_no_table(self):
        self.client.force_login(self.alice)
        resp = self.post_booking(3)
        self.assertEqual(resp.status_code, 302)
        booking = TableBooking.objects.get()
        self.assertIsNone(booking.table)                  # the admin chooses the table
        self.assertEqual(booking.status, 'pending')
        # a pending request holds nothing
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertEqual(table_status_map(), {})

    def test_no_table_error_when_slot_is_full(self):
        # both tables are held by approved bookings that the admin has not opened
        self.book(self.alice, at(19, 0), table=self.t4)
        self.book(self.bob, at(19, 0), table=self.t6)
        self.client.force_login(self.alice)
        resp = self.post_booking(2, time='20:00')
        self.assertEqual(resp.status_code, 200)            # form re-rendered with error
        self.assertEqual(TableBooking.objects.count(), 2)

    def test_check_availability_endpoint(self):
        self.client.force_login(self.alice)
        self.book(self.bob, at(19, 0), table=self.t4)
        self.book(self.bob, at(19, 0), table=self.t6)
        url = reverse('check_availability')
        day = TEST_DAY.isoformat()

        # later the same day: both tables are still held (no timer frees them)
        full = self.client.get(url, {'date': day, 'time': '21:00', 'guests': 2}).json()
        self.assertFalse(full['available'])
        # before the bookings start: free
        early = self.client.get(url, {'date': day, 'time': '18:00', 'guests': 2}).json()
        self.assertTrue(early['available'])

        # once the admin opens one table, a later slot has a free table again
        booking = TableBooking.objects.filter(table=self.t4).get()
        booking.released = True
        booking.save(update_fields=['released'])
        again = self.client.get(url, {'date': day, 'time': '21:00', 'guests': 2}).json()
        self.assertTrue(again['available'])

    def test_check_availability_big_party_needs_two_tables(self):
        self.client.force_login(self.alice)
        url = reverse('check_availability')
        day = TEST_DAY.isoformat()

        both_free = self.client.get(url, {'date': day, 'time': '20:00', 'guests': 8}).json()
        self.assertTrue(both_free['available'])
        self.assertEqual(both_free['tables_needed'], 2)

        self.book(self.bob, at(19, 0), table=self.t6)      # one table is now held
        one_left = self.client.get(url, {'date': day, 'time': '20:00', 'guests': 8}).json()
        self.assertFalse(one_left['available'])
        self.assertEqual(one_left['tables_needed'], 2)

    def test_six_guests_need_one_table_seven_need_two(self):
        self.client.force_login(self.alice)
        url = reverse('check_availability')
        day = TEST_DAY.isoformat()

        six = self.client.get(url, {'date': day, 'time': '20:00', 'guests': 6}).json()
        self.assertTrue(six['available'])
        self.assertEqual(six['tables_needed'], 1)

        seven = self.client.get(url, {'date': day, 'time': '20:00', 'guests': 7}).json()
        self.assertTrue(seven['available'])
        self.assertEqual(seven['tables_needed'], 2)

        self.book(self.bob, at(19, 0), table=self.t6)      # one table held -> 7 guests can't fit
        seven_again = self.client.get(url, {'date': day, 'time': '20:00', 'guests': 7}).json()
        self.assertFalse(seven_again['available'])
        self.assertEqual(seven_again['tables_needed'], 2)

    # ---- no timer ----
    def test_approved_booking_holds_table_until_admin_opens_it(self):
        self.book(self.alice, at(19, 0), table=self.t4)

        with patch('django.utils.timezone.now', return_value=at(18, 59)):
            self.assertNotIn(self.t4.id, table_status_map())   # not started yet
        with patch('django.utils.timezone.now', return_value=at(19, 0)):
            self.assertIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(21, 0)):
            self.assertIn(self.t4.id, table_status_map())      # no 2 hour auto open
        with patch('django.utils.timezone.now', return_value=at(19, 0) + timedelta(hours=10)):
            self.assertIn(self.t4.id, table_status_map())      # still held the next morning

    def test_pending_and_rejected_bookings_hold_nothing(self):
        self.book(self.alice, at(19, 0), table=self.t4, status='pending')
        self.book(self.bob, at(19, 0), table=self.t6, status='rejected')
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertEqual(table_status_map(), {})
            self.assertFalse(is_table_busy_now(self.t4.id, self.bob))
        self.assertEqual(set(free_tables_for_slot(at(20, 0), 3)), {self.t4, self.t6})

    def test_walk_in_blocked_shortly_before_an_approved_booking(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        with patch('django.utils.timezone.now', return_value=at(18, 40)):
            self.assertTrue(is_table_busy_now(self.t4.id, self.bob))        # starts within 30 min
            self.assertFalse(is_table_busy_now(self.t4.id, self.alice))     # the booker is fine
        with patch('django.utils.timezone.now', return_value=at(18, 0)):
            self.assertFalse(is_table_busy_now(self.t4.id, self.bob))

    def test_cart_shows_table_as_booked_for_others_but_not_for_the_holder(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            for_bob = {t['id']: t['is_booked'] for t in _get_tables_with_status(self.bob)}
            for_alice = {t['id']: t['is_booked'] for t in _get_tables_with_status(self.alice)}
        self.assertTrue(for_bob[self.t4.id])
        self.assertFalse(for_alice[self.t4.id])
        self.assertFalse(for_bob[self.t6.id])

    # ---- admin open ----
    def test_admin_open_table_releases_the_hold(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        self.admin_client()

        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertTrue(is_table_busy_now(self.t4.id, self.bob))
            resp = self.client.post(reverse('dashboard:open_table', args=[self.t4.pk]),
                                    {'next': 'manage_tables'})
            self.assertEqual(resp.status_code, 302)
            self.assertFalse(is_table_busy_now(self.t4.id, self.bob))
            self.assertIn(self.t4, free_tables_for_slot(at(19, 30), 3))

        b = TableBooking.objects.get()
        self.assertTrue(b.released)
        self.assertIsNotNone(b.released_at)

    def test_open_table_needs_admin_and_post(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        self.client.force_login(self.bob)
        resp = self.client.post(reverse('dashboard:open_table', args=[self.t4.pk]))
        self.assertEqual(resp.status_code, 302)            # bounced to login
        self.assertFalse(TableBooking.objects.get().released)

        self.admin_client()
        self.assertEqual(self.client.get(reverse('dashboard:open_table', args=[self.t4.pk])).status_code, 405)

    def test_manage_pages_show_hold_without_timer_text(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        self.admin_client()
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            r1 = self.client.get(reverse('dashboard:manage_tables'))
            r2 = self.client.get(reverse('dashboard:manage_bookings'))
        self.assertContains(r1, 'until you open it')
        self.assertNotContains(r1, 'Opens automatically at')
        self.assertContains(r2, 'until you open it')
        self.assertContains(r2, 'Open Table')

    # ---- admin approves and chooses the table(s) ----
    def test_manage_bookings_suggests_two_tables_for_eight_guests(self):
        self.book(self.alice, at(19, 0), guests=8, table=None, status='pending')
        self.admin_client()
        resp = self.client.get(reverse('dashboard:manage_bookings'))
        self.assertContains(resp, 'at least')

    def test_manage_bookings_suggests_two_tables_for_seven_guests(self):
        self.book(self.alice, at(19, 0), guests=7, table=None, status='pending')
        self.admin_client()
        resp = self.client.get(reverse('dashboard:manage_bookings'))
        self.assertContains(resp, 'at least')

    def test_admin_approve_assigns_chosen_table(self):
        booking = self.book(self.alice, at(19, 0), guests=3, table=None, status='pending')
        self.admin_client()
        resp = self.client.post(reverse('dashboard:approve_booking', args=[booking.pk]),
                                {'tables': [self.t4.id]})
        self.assertEqual(resp.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'approved')
        self.assertEqual(booking.table, self.t4)
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertIn(self.t4.id, table_status_map())

    def test_admin_approve_needs_a_table_and_enough_seats(self):
        booking = self.book(self.alice, at(19, 0), guests=8, table=None, status='pending')
        self.admin_client()
        url = reverse('dashboard:approve_booking', args=[booking.pk])

        self.client.post(url, {})                              # no table chosen
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'pending')

        self.client.post(url, {'tables': [self.t4.id]})        # 4 seats for 8 guests
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'pending')

    def test_admin_approve_seven_guests_needs_enough_seats(self):
        booking = self.book(self.alice, at(19, 0), guests=7, table=None, status='pending')
        self.admin_client()
        url = reverse('dashboard:approve_booking', args=[booking.pk])

        self.client.post(url, {'tables': [self.t6.id]})        # 6 seats for 7 guests -> refused
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'pending')

        self.client.post(url, {'tables': [self.t4.id, self.t6.id]})   # 2 tables, 10 seats
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'approved')

    def test_admin_approve_seven_guests_with_one_seven_seat_table(self):
        t7 = Table.objects.create(number=3, capacity=7)
        booking = self.book(self.alice, at(19, 0), guests=7, table=None, status='pending')
        self.admin_client()
        self.client.post(reverse('dashboard:approve_booking', args=[booking.pk]),
                         {'tables': [t7.id]})
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'approved')           # 7 seats cover 7 guests
        self.assertEqual(booking.table, t7)
        self.assertEqual(list(booking.extra_tables.all()), [])

    def test_admin_approve_big_party_holds_every_table_and_open_frees_all(self):
        t6b = Table.objects.create(number=3, capacity=6)
        booking = self.book(self.alice, at(19, 0), guests=8, table=None, status='pending')
        self.admin_client()
        self.client.post(reverse('dashboard:approve_booking', args=[booking.pk]),
                         {'tables': [self.t6.id, t6b.id]})
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'approved')
        self.assertEqual(booking.table, self.t6)               # lowest number is the main table
        self.assertEqual(list(booking.extra_tables.all()), [t6b])

        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            held = table_status_map()
            self.assertIn(self.t6.id, held)
            self.assertIn(t6b.id, held)
            self.assertEqual(release_table(self.t6.id), 1)      # opening one opens the booking
            self.assertEqual(table_status_map(), {})

    def test_admin_approve_warns_about_a_held_table_unless_forced(self):
        self.book(self.alice, at(19, 0), table=self.t6)         # approved, not opened yet
        later = self.book(self.bob, at(20, 0), guests=3, table=None, status='pending')
        self.admin_client()
        url = reverse('dashboard:approve_booking', args=[later.pk])

        self.client.post(url, {'tables': [self.t6.id]})
        later.refresh_from_db()
        self.assertEqual(later.status, 'pending')              # refused: t6 is still held

        self.client.post(url, {'tables': [self.t6.id], 'force': '1'})
        later.refresh_from_db()
        self.assertEqual(later.status, 'approved')

    def test_admin_approve_warns_when_table_is_reserved_for_a_later_booking(self):
        self.book(self.alice, at(16, 50), table=self.t6)        # approved, starts LATER
        earlier = self.book(self.bob, at(16, 0), guests=3, table=None, status='pending')
        self.admin_client()
        url = reverse('dashboard:approve_booking', args=[earlier.pk])

        self.client.post(url, {'tables': [self.t6.id]})
        earlier.refresh_from_db()
        self.assertEqual(earlier.status, 'pending')            # refused: t6 is reserved at 4:50 PM

        self.client.post(url, {'tables': [self.t4.id]})        # a free table is fine
        earlier.refresh_from_db()
        self.assertEqual(earlier.status, 'approved')

    def test_suggestion_uses_one_big_table_for_seven_guests(self):
        from dashboard.views import _suggest_table_ids
        t7 = Table.objects.create(number=3, capacity=7)
        tables = list(Table.objects.order_by('capacity', 'number'))
        self.assertEqual(_suggest_table_ids(7, {}, tables), [t7.id])
        # when the 7 seat table is taken, fall back to two tables
        self.assertEqual(set(_suggest_table_ids(7, {t7.id: 'held'}, tables)), {self.t4.id, self.t6.id})

    def test_all_tables_sorted_and_seats_summary(self):
        # main table has the HIGHER number on purpose: all_tables must still sort by number
        b = self.book(self.alice, at(19, 0), guests=9, table=self.t6, extra=[self.t4])
        self.assertEqual([t.number for t in b.all_tables], [1, 2])
        self.assertEqual(b.table_numbers, 'Tables 1, 2')
        self.assertEqual(b.total_seats, 10)
        self.assertEqual(b.seats_summary, '4 + 6 = 10 seats')

        t6b = Table.objects.create(number=3, capacity=6)
        same = self.book(self.bob, at(21, 0), guests=9, table=self.t6, extra=[t6b])
        self.assertEqual(same.seats_summary, '6 seats per table')

        none = self.book(self.bob, at(22, 0), table=None, status='pending')
        self.assertEqual(none.table_numbers, '')
        self.assertFalse(none.has_tables)

    def test_admin_reject_booking(self):
        booking = self.book(self.alice, at(19, 0), table=None, status='pending')
        self.admin_client()
        resp = self.client.post(reverse('dashboard:reject_booking', args=[booking.pk]),
                                {'reason': 'Fully booked'})
        self.assertEqual(resp.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'rejected')

    # ---- dine-in orders keep the table closed until the order is finished ----
    def _order(self, user, status, created):
        o = Order.objects.create(user=user, table=self.t4, status=status, total_amount=100)
        Order.objects.filter(pk=o.pk).update(created_at=created)
        return Order.objects.get(pk=o.pk)

    def test_live_order_keeps_table_closed_until_finished(self):
        o = self._order(self.alice, 'confirmed', at(19, 0))
        with patch('django.utils.timezone.now', return_value=at(20, 59)):
            self.assertIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(21, 1)):
            self.assertIn(self.t4.id, table_status_map())      # order still live
        Order.objects.filter(pk=o.pk).update(status='delivered')
        with patch('django.utils.timezone.now', return_value=at(21, 1)):
            self.assertIn(self.t4.id, table_status_map())      # served, customer still seated
        Order.objects.filter(pk=o.pk).update(table_released=True)
        with patch('django.utils.timezone.now', return_value=at(21, 1)):
            self.assertNotIn(self.t4.id, table_status_map())   # Customer Left -> opens

    def test_pending_unpaid_order_only_holds_briefly(self):
        self._order(self.alice, 'pending', at(19, 0))
        with patch('django.utils.timezone.now', return_value=at(19, 10)):
            self.assertIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(19, 20)):
            self.assertNotIn(self.t4.id, table_status_map())

    def test_admin_can_open_table_held_by_an_order(self):
        o = self._order(self.alice, 'preparing', at(19, 0))
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertEqual(release_table(self.t4.id), 1)
            self.assertNotIn(self.t4.id, table_status_map())
        o.refresh_from_db()
        self.assertTrue(o.table_released)
        self.assertEqual(o.status, 'preparing')            # order itself untouched

    def test_cancelled_order_does_not_hold_table(self):
        self._order(self.alice, 'cancelled', at(19, 0))
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertNotIn(self.t4.id, table_status_map())

class BugFixRegressionTests(TestCase):
    """Cancel page, privacy, seat check, decided_at, served-order hold."""

    def setUp(self):
        self.t1 = Table.objects.create(number=1, capacity=4)
        self.alice = User.objects.create_user('alice', password='x', email='a@x.com')
        self.admin = User.objects.create_superuser('admin', password='x')
        self.day = timezone.localdate() + timedelta(days=3)

    def _booking(self, status='pending', table=None, guests=2):
        return TableBooking.objects.create(
            user=self.alice, name='Alice', phone='9876543210', guests=guests,
            booking_date=self.day, booking_time='19:00', table=table, status=status)

    def _post(self, guests):
        return self.client.post(reverse('book_table'), {
            'name': 'A', 'phone': '9876543210', 'guests': guests,
            'booking_date': self.day.isoformat(), 'booking_time': '19:00',
            'occasion': 'none', 'special_request': ''})

    def test_cancel_page_opens(self):
        b = self._booking()
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse('cancel_booking', args=[b.id])).status_code, 200)

    def test_bookings_for_date_hides_names(self):
        self._booking(status='approved', table=self.t1)
        self.client.force_login(self.alice)
        data = self.client.get(reverse('bookings_for_date'), {'date': self.day.isoformat()}).json()
        self.assertEqual(len(data['bookings']), 1)
        self.assertNotIn('name', data['bookings'][0])
        self.assertNotIn('occasion', data['bookings'][0])

    def test_bookings_for_date_bad_date_is_not_500(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse('bookings_for_date'), {'date': 'abc'}).status_code, 200)

    def test_one_big_table_seats_seven_guests(self):
        Table.objects.all().delete()
        Table.objects.create(number=1, capacity=7)
        self.client.force_login(self.alice)
        self.assertEqual(self._post(7).status_code, 302)

    def test_not_enough_seats_refused(self):
        Table.objects.all().delete()
        Table.objects.create(number=1, capacity=2)
        Table.objects.create(number=2, capacity=2)
        self.client.force_login(self.alice)
        self.assertEqual(self._post(12).status_code, 200)
        self.assertEqual(TableBooking.objects.count(), 0)

    def test_approve_and_reject_save_decided_at(self):
        b1, b2 = self._booking(), self._booking()
        self.client.force_login(self.admin)
        self.client.post(reverse('dashboard:approve_booking', args=[b1.id]), {'tables': [self.t1.id]})
        self.client.post(reverse('dashboard:reject_booking', args=[b2.id]), {'reason': 'full'})
        b1.refresh_from_db(); b2.refresh_from_db()
        self.assertIsNotNone(b1.decided_at)
        self.assertIsNotNone(b2.decided_at)
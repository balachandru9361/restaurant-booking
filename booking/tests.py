from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, FoodItem
from orders.models import Order
from orders.views import _get_tables_with_status
from .availability import (
    free_tables_for_slot, is_table_busy_now, release_table, table_status_map,
)
from .models import Table, TableBooking


def at(hour, minute=0, day=20):
    """Aware datetime on 2026-09-<day> at hour:minute (local / IST)."""
    return timezone.make_aware(datetime(2026, 9, day, hour, minute))


class TableHoldTests(TestCase):
    def setUp(self):
        self.t4 = Table.objects.create(number=1, capacity=4)
        self.t6 = Table.objects.create(number=2, capacity=6)
        self.alice = User.objects.create_user('alice', password='x')
        self.bob = User.objects.create_user('bob', password='x')

    def book(self, user, when, guests=3, table=None):
        return TableBooking.objects.create(
            user=user, name=user.username, phone='9876543210', guests=guests,
            booking_date=when.date(), booking_time=when.time(), table=table,
        )

    # ---- booking flow ----
    def test_booking_assigns_best_fit_table_and_holds_it_for_two_hours(self):
        self.client.force_login(self.alice)
        resp = self.client.post(reverse('book_table'), {
            'name': 'Alice', 'phone': '9876543210', 'guests': 3,
            'booking_date': '2026-09-20', 'booking_time': '19:00',
            'occasion': 'none', 'special_request': '',
        })
        self.assertEqual(resp.status_code, 302)
        booking = TableBooking.objects.get()
        self.assertEqual(booking.table, self.t4)          # smallest table that fits 3

    def test_overlapping_slots_get_other_table_then_reopen_after_two_hours(self):
        self.book(self.alice, at(19, 0), table=self.t4)

        # 20:00 overlaps 19:00-21:00 -> t4 is hidden, t6 is offered
        self.assertEqual(free_tables_for_slot(at(20, 0), 3), [self.t6])
        self.book(self.bob, at(20, 0), table=self.t6)

        # 20:30 -> everything held
        self.assertEqual(free_tables_for_slot(at(20, 30), 3), [])
        # 21:00 -> t4's 2 hours are over, it opens automatically
        self.assertEqual(free_tables_for_slot(at(21, 0), 3), [self.t4])

    def test_no_table_error_when_slot_is_full(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        self.book(self.bob, at(19, 0), table=self.t6)
        self.client.force_login(self.alice)
        resp = self.client.post(reverse('book_table'), {
            'name': 'Alice', 'phone': '9876543210', 'guests': 2,
            'booking_date': '2026-09-20', 'booking_time': '20:00',
            'occasion': 'none', 'special_request': '',
        })
        self.assertEqual(resp.status_code, 200)            # form re-rendered with error
        self.assertEqual(TableBooking.objects.count(), 2)

    def test_check_availability_endpoint(self):
        self.client.force_login(self.alice)
        self.book(self.bob, at(19, 0), table=self.t4)
        self.book(self.bob, at(19, 0), table=self.t6)
        url = reverse('check_availability')
        full = self.client.get(url, {'date': '2026-09-20', 'time': '20:00', 'guests': 2}).json()
        self.assertFalse(full['available'])
        free = self.client.get(url, {'date': '2026-09-20', 'time': '21:00', 'guests': 2}).json()
        self.assertTrue(free['available'])

    # ---- hide now / auto open ----
    def test_table_hidden_during_window_and_opens_automatically(self):
        self.book(self.alice, at(19, 0), table=self.t4)

        with patch('django.utils.timezone.now', return_value=at(18, 59)):
            self.assertNotIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(19, 0)):
            self.assertIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(20, 59)):
            self.assertIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(21, 0)):
            self.assertNotIn(self.t4.id, table_status_map())   # auto open

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
        admin = User.objects.create_superuser('admin', password='x')
        self.client.force_login(admin)

        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            self.assertTrue(is_table_busy_now(self.t4.id, self.bob))
            resp = self.client.post(reverse('dashboard:open_table', args=[self.t4.pk]),
                                    {'next': 'manage_tables'})
            self.assertEqual(resp.status_code, 302)
            self.assertFalse(is_table_busy_now(self.t4.id, self.bob))
            # and the slot can be booked again by someone else
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

        admin = User.objects.create_superuser('admin', password='x')
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('dashboard:open_table', args=[self.t4.pk])).status_code, 405)

    def test_manage_tables_and_bookings_pages_render(self):
        self.book(self.alice, at(19, 0), table=self.t4)
        admin = User.objects.create_superuser('admin', password='x')
        self.client.force_login(admin)
        with patch('django.utils.timezone.now', return_value=at(19, 30)):
            r1 = self.client.get(reverse('dashboard:manage_tables'))
            r2 = self.client.get(reverse('dashboard:manage_bookings'))
        self.assertContains(r1, 'Open Table')
        self.assertContains(r1, 'Opens automatically at')
        self.assertContains(r2, 'Table held until')
        self.assertContains(r2, 'Open Table')

    # ---- dine-in orders also hold the table for 2 hours ----
    def _order(self, user, status, created):
        o = Order.objects.create(user=user, table=self.t4, status=status, total_amount=100)
        Order.objects.filter(pk=o.pk).update(created_at=created)
        return Order.objects.get(pk=o.pk)

    def test_dine_in_order_holds_table_two_hours(self):
        self._order(self.alice, 'confirmed', at(19, 0))
        with patch('django.utils.timezone.now', return_value=at(20, 59)):
            self.assertIn(self.t4.id, table_status_map())
        with patch('django.utils.timezone.now', return_value=at(21, 1)):
            self.assertNotIn(self.t4.id, table_status_map())

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

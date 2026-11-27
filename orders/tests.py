from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from unittest.mock import patch

from accounts.models import Profile
from booking.models import SeatAllocation, Table, TableSession
from dashboard.views import _table_bill_orders
from menu.models import Category, FoodItem
from .cart_state import QR_SESSION_COOKIE
from .models import Bill, Order
from dashboard.models import Notification


class CustomerOrderCancellationTests(TestCase):
    def setUp(self):
        self.table = Table.objects.create(number=92, capacity=6)
        self.table_session = TableSession.objects.create(
            table=self.table,
            guests=2,
            guest_count=2,
            status=TableSession.STATUS_ORDERING,
        )
        self.order = Order.objects.create(
            guest_name='QR Customer',
            guest_phone='1234567890',
            table=self.table,
            table_session=self.table_session,
            source='qr',
            payment_method='online',
            payment_status='pending',
        )
        session = self.client.session
        session['guest_order_ids'] = [self.order.pk]
        session.save()

    def cancel(self, client=None, **data):
        return (client or self.client).post(
            reverse('orders:cancel_customer_order', args=[self.order.pk]),
            data,
        )

    def test_customer_can_cancel_before_preparation_and_release_its_session(self):
        page = self.client.get(reverse('orders:order_history'))
        self.assertContains(page, 'Cancel Order')
        self.assertContains(page, 'Emergency Situation')
        self.assertContains(page, 'Other (Enter your reason)')

        response = self.cancel(reason_choice='Emergency Situation')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['reason'], 'Emergency Situation')
        self.order.refresh_from_db()
        self.table_session.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.order.cancellation_reason, 'Emergency Situation')
        self.assertIsNotNone(self.order.cancelled_at)
        self.assertFalse(self.table_session.is_active)
        self.assertEqual(self.table_session.status, TableSession.STATUS_CANCELLED)

    def test_other_reason_is_required_and_saved_verbatim(self):
        missing_reason = self.cancel(reason_choice='other', other_reason='   ')
        self.assertEqual(missing_reason.status_code, 400)
        self.assertEqual(missing_reason.json()['ok'], False)

        response = self.cancel(
            reason_choice='other',
            other_reason='Our plans changed at the last minute',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.cancellation_reason,
            'Our plans changed at the last minute',
        )

    @patch('orders.services.refund_razorpay_payment', return_value=(True, ''))
    def test_paid_confirmed_order_is_refunded_and_kitchen_is_notified(self, refund):
        self.order.status = 'confirmed'
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['status', 'payment_status'])

        response = self.cancel(reason_choice='Long Waiting Time')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['refunded'])
        refund.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'paid')
        self.assertEqual(self.order.refund_status, 'refunded')
        self.assertTrue(
            Notification.objects.filter(
                kind='cancelled', order=self.order,
                message__contains='Long Waiting Time',
            ).exists()
        )

    def test_preparing_order_requires_approval_and_duplicate_request_is_blocked(self):
        self.order.status = 'preparing'
        self.order.save(update_fields=['status'])
        preparing = self.cancel(reason_choice='Emergency Situation')
        self.assertEqual(preparing.status_code, 200)
        self.assertFalse(preparing.json()['cancelled'])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'preparing')
        self.assertEqual(self.order.cancellation_status, 'pending')
        duplicate = self.cancel(reason_choice='Emergency Situation')
        self.assertEqual(duplicate.status_code, 409)

    def test_cannot_cancel_order_from_another_browser_session(self):
        response = self.cancel(client=Client(), reason_choice='Duplicate Order')
        self.assertEqual(response.status_code, 404)

    def test_server_approval_releases_only_the_cancelled_customer_session(self):
        self.table_session.guest_count = None
        self.table_session.save(update_fields=['guest_count'])
        SeatAllocation.objects.create(
            session=self.table_session, table=self.table, seats=2,
            status=SeatAllocation.OCCUPIED,
        )
        other_session = TableSession.objects.create(
            table=self.table, guests=2, source=TableSession.SOURCE_QR,
        )
        SeatAllocation.objects.create(
            session=other_session, table=self.table, seats=2,
            status=SeatAllocation.OCCUPIED,
        )
        Order.objects.create(
            table=self.table, table_session=other_session, source='qr',
            payment_method='online', payment_status='paid', status='confirmed',
        )
        self.order.status = 'ready'
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['status', 'payment_status'])
        self.assertEqual(self.table.seats_free, 2)

        submitted = self.cancel(reason_choice='Long Waiting Time')
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.assertFalse(submitted.json()['cancelled'])
        self.assertEqual(self.table.seats_free, 2)

        server = User.objects.create_user('cancellation-server', password='x', is_staff=True)
        Profile.objects.create(user=server, role='server')
        server_client = Client()
        server_client.force_login(server)
        dashboard = server_client.get(reverse('dashboard:server_dashboard'))
        self.assertContains(dashboard, 'Pending Cancellation Requests')
        self.assertContains(dashboard, 'Long Waiting Time')
        self.assertContains(dashboard, f'Order #{self.order.pk}')

        with patch('orders.services.refund_razorpay_payment', return_value=(True, '')):
            approved = server_client.post(
                reverse('dashboard:server_decide_cancellation', args=[self.order.pk]),
                {'decision': 'approve'},
            )
        self.assertEqual(approved.status_code, 302)
        self.order.refresh_from_db()
        self.table_session.refresh_from_db()
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.order.cancellation_status, 'approved')
        self.assertEqual(self.order.refund_status, 'refunded')
        self.assertFalse(self.table_session.is_active)
        self.assertEqual(
            self.table_session.allocations.get().status,
            SeatAllocation.RELEASED,
        )
        self.assertTrue(other_session.is_active)
        self.assertEqual(self.table.seats_free, 4)

    def test_server_rejection_keeps_order_and_seat_allocation_unchanged(self):
        allocation = SeatAllocation.objects.create(
            session=self.table_session, table=self.table, seats=2,
            status=SeatAllocation.OCCUPIED,
        )
        self.order.status = 'preparing'
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['status', 'payment_status'])
        self.assertEqual(self.cancel(reason_choice='Emergency Situation').status_code, 200)

        server = User.objects.create_user('reject-cancellation-server', password='x', is_staff=True)
        Profile.objects.create(user=server, role='server')
        server_client = Client()
        server_client.force_login(server)
        rejected = server_client.post(
            reverse('dashboard:server_decide_cancellation', args=[self.order.pk]),
            {'decision': 'reject'},
        )
        self.assertEqual(rejected.status_code, 302)
        self.order.refresh_from_db()
        self.table_session.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual(self.order.status, 'preparing')
        self.assertEqual(self.order.cancellation_status, 'rejected')
        self.assertTrue(self.table_session.is_active)
        self.assertEqual(allocation.status, SeatAllocation.OCCUPIED)

        admin = User.objects.create_superuser('history-admin', password='x')
        admin_client = Client()
        admin_client.force_login(admin)
        history = admin_client.get(
            reverse('dashboard:manage_orders'), {'filter': 'cancellations'}
        )
        self.assertContains(history, 'Cancellation history')
        self.assertContains(history, 'Cancellation decision: Rejected')
        self.assertContains(history, 'Emergency Situation')

    def test_served_qr_order_cannot_use_customer_cancellation(self):
        self.order.status = 'delivered'
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['status', 'payment_status'])

        response = self.cancel(reason_choice='Change of Plans')

        self.assertEqual(response.status_code, 409)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'delivered')
        self.assertEqual(self.order.cancellation_status, 'not_requested')

    def test_cancelling_one_order_keeps_seats_until_the_session_has_no_active_orders(self):
        allocation = SeatAllocation.objects.create(
            session=self.table_session, table=self.table, seats=2,
            status=SeatAllocation.OCCUPIED,
        )
        sibling = Order.objects.create(
            guest_name='QR Customer',
            guest_phone='1234567890',
            table=self.table,
            table_session=self.table_session,
            source='qr',
            payment_method='online',
            payment_status='paid',
            status='confirmed',
        )
        session = self.client.session
        session['guest_order_ids'] = [self.order.pk, sibling.pk]
        session.save()

        cancelled_first = self.cancel(reason_choice='Ordered by Mistake')
        self.assertEqual(cancelled_first.status_code, 200, cancelled_first.content)
        allocation.refresh_from_db()
        self.table_session.refresh_from_db()
        self.assertEqual(allocation.status, SeatAllocation.OCCUPIED)
        self.assertTrue(self.table_session.is_active)

        cancelled_last = self.client.post(
            reverse('orders:cancel_customer_order', args=[sibling.pk]),
            {'reason_choice': 'Duplicate Order'},
        )
        self.assertEqual(cancelled_last.status_code, 200, cancelled_last.content)
        allocation.refresh_from_db()
        self.table_session.refresh_from_db()
        self.assertEqual(allocation.status, SeatAllocation.RELEASED)
        self.assertFalse(self.table_session.is_active)


class QRGuestSessionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.table = Table.objects.create(number=91, capacity=6)
        category = Category.objects.create(name='QR Test', slug='qr-test')
        self.food = FoodItem.objects.create(
            category=category,
            name='QR Test Dish',
            price='10.00',
        )

    def scan(self, client):
        response = client.get(
            reverse('orders:select_table', args=[self.table.pk])
        )
        self.assertEqual(response.status_code, 302)
        token = client.cookies[QR_SESSION_COOKIE].value
        session = TableSession.objects.get(qr_token=token)
        self.assertTrue(Bill.objects.filter(table_session=session).exists())
        return session

    def add_item(self, client, quantity=1):
        return client.post(
            reverse('orders:add_to_cart', args=[self.food.pk]),
            {'quantity': quantity},
        )

    def place_order(self, client, guests, name, customer_type='other'):
        return client.post(
            reverse('orders:confirm_order'),
            {
                'order_type': 'dine_in',
                'guests': guests,
                'customer_type': customer_type,
                'guest_name': name,
                'guest_phone': '1234567890',
            },
        )

    def test_groups_get_remaining_options_and_keep_cart_order_and_bill_separate(self):
        client_a = Client()
        session_a = self.scan(client_a)
        self.add_item(client_a)
        client_b = Client()
        session_b = self.scan(client_b)
        self.add_item(client_b, quantity=2)

        cart_a = client_a.get(reverse('orders:cart_data')).json()
        cart_b = client_b.get(reverse('orders:cart_data')).json()
        self.assertEqual(cart_a['items'][0]['quantity'], 1)
        self.assertEqual(cart_b['items'][0]['quantity'], 2)

        response_a = self.place_order(client_a, 3, 'Group A', 'family')
        self.assertIn('/payment/initiate/', response_a.url)

        session_a.refresh_from_db()
        self.assertEqual(session_a.guest_count, 3)
        self.assertEqual(session_a.customer_type, 'family')
        self.assertEqual(session_a.status, TableSession.STATUS_ORDERING)
        self.assertEqual(self.table.seats_free, 3)
        order_a = Order.objects.get(table_session=session_a)
        self.assertEqual(order_a.guests, 3)

        cart_a = client_a.get(reverse('orders:cart_data')).json()
        self.assertEqual(cart_a['items'], [])

        cart_page = client_b.get(reverse('orders:view_cart'))
        self.assertContains(cart_page, '<option value="1"')
        self.assertContains(cart_page, '<option value="2"')
        self.assertContains(cart_page, '<option value="3"')
        self.assertNotContains(cart_page, '<option value="4"')

        response_b = self.place_order(client_b, 3, 'Group B', 'friends')
        self.assertIn('/payment/initiate/', response_b.url)
        session_b.refresh_from_db()
        self.assertEqual(session_b.guest_count, 3)
        self.assertEqual(session_b.customer_type, 'friends')
        self.assertEqual(self.table.seats_free, 0)

        client_c = Client()
        session_c = self.scan(client_c)
        self.add_item(client_c)
        cart_page_c = client_c.get(reverse('orders:view_cart'))
        self.assertContains(cart_page_c, 'No seats available.')
        response_c = self.place_order(client_c, 1, 'Group C')
        self.assertEqual(response_c.url, reverse('orders:view_cart'))
        self.assertEqual(Order.objects.filter(table_session=session_c).count(), 0)

        orders_a = Order.objects.filter(table_session=session_a)
        orders_b = Order.objects.filter(table_session=session_b)
        self.assertEqual(orders_a.count(), 1)
        self.assertEqual(orders_b.count(), 1)
        self.assertNotEqual(orders_a.first().pk, orders_b.first().pk)
        Order.objects.filter(pk__in=[orders_a.first().pk, orders_b.first().pk]).update(
            payment_status='paid',
            status='confirmed',
        )
        self.assertEqual([o.pk for o in _table_bill_orders(orders_a.first())],
                         [orders_a.first().pk])
        self.assertEqual([o.pk for o in _table_bill_orders(orders_b.first())],
                         [orders_b.first().pk])
        self.assertEqual(Bill.objects.filter(table_session__table=self.table).count(), 3)
        self.assertEqual(Bill.objects.filter(table_session=session_a).count(), 1)
        self.assertEqual(Bill.objects.filter(table_session=session_b).count(), 1)

    def test_only_one_concurrent_order_can_reserve_the_last_seats(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock concurrency requires PostgreSQL.')

        self.table.capacity = 2
        self.table.save(update_fields=['capacity'])
        clients = [Client(), Client()]
        for client in clients:
            self.scan(client)
            self.add_item(client)

        barrier = Barrier(2)

        def submit(client, name):
            close_old_connections()
            try:
                barrier.wait()
                return self.place_order(client, 2, name)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda args: submit(*args),
                [(clients[0], 'Concurrent A'), (clients[1], 'Concurrent B')],
            ))

        payment_responses = [
            response for response in responses
            if '/payment/initiate/' in response.url
        ]
        self.assertEqual(len(payment_responses), 1)
        self.assertEqual(
            TableSession.objects.filter(
                table=self.table,
                is_active=True,
                guest_count__isnull=False,
            ).count(),
            1,
        )
        self.assertEqual(Order.objects.filter(table=self.table).count(), 1)

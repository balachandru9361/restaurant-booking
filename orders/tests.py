from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from booking.models import Table, TableSession
from dashboard.views import _table_bill_orders
from menu.models import Category, FoodItem
from .cart_state import QR_SESSION_COOKIE
from .models import Bill, Order


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

    def place_order(self, client, guests, name):
        return client.post(
            reverse('orders:confirm_order'),
            {
                'order_type': 'dine_in',
                'guests': guests,
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

        response_a = self.place_order(client_a, 3, 'Group A')
        self.assertIn('/payment/initiate/', response_a.url)

        session_a.refresh_from_db()
        self.assertEqual(session_a.guest_count, 3)
        order_a = Order.objects.get(table_session=session_a)
        self.assertEqual(order_a.guests, 3)

        cart_a = client_a.get(reverse('orders:cart_data')).json()
        self.assertEqual(cart_a['items'], [])

        cart_page = client_b.get(reverse('orders:view_cart'))
        self.assertContains(cart_page, '<option value="1"')
        self.assertContains(cart_page, '<option value="2"')
        self.assertContains(cart_page, '<option value="3"')
        self.assertNotContains(cart_page, '<option value="4"')

        response_b = self.place_order(client_b, 3, 'Group B')
        self.assertIn('/payment/initiate/', response_b.url)
        session_b.refresh_from_db()
        self.assertEqual(session_b.guest_count, 3)

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

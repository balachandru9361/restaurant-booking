import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile
from booking.models import SeatAllocation, Table, TableSession
from menu.models import Category, FoodItem
from orders.models import Coupon, Order, OrderItem
from .models import Notification


def _seat_compositions(total):
    if total == 0:
        return [()]
    return [
        (first, *rest)
        for first in range(1, total + 1)
        for rest in _seat_compositions(total - first)
    ]


class SharedTableGroupSeatingTests(TestCase):
    def setUp(self):
        self.server = User.objects.create_user('seating-server', password='x', is_staff=True)
        Profile.objects.create(user=self.server, role='server')
        self.client.force_login(self.server)
        self.table = Table.objects.create(number=75, capacity=6)
        self.seat_url = reverse('dashboard:server_walkin_confirm')

    def seat(self, guests, customer_type='other', group_name=''):
        return self.client.post(
            self.seat_url,
            json.dumps({
                'guests': guests,
                'customer_type': customer_type,
                'group_name': group_name,
                'plan': [{'table_id': self.table.pk, 'seats': guests}],
            }),
            content_type='application/json',
        )

    def test_all_ordered_seat_combinations_fit_without_merging_groups(self):
        for combination in _seat_compositions(self.table.capacity):
            with self.subTest(combination=combination):
                SeatAllocation.objects.filter(table=self.table).delete()
                for guests in combination:
                    response = self.seat(guests)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertTrue(response.json()['group_id'].startswith('G-'))

                self.table.refresh_from_db()
                self.assertEqual(self.table.seats_taken, self.table.capacity)
                self.assertEqual(self.table.seats_free, 0)
                self.assertEqual(
                    self.table.allocations.filter(status=SeatAllocation.OCCUPIED).count(),
                    len(combination),
                )

    def test_capacity_is_rechecked_and_only_departing_group_seats_are_released(self):
        first = self.seat(4, customer_type='family', group_name='Group A').json()
        rejected = self.seat(3)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(TableSession.objects.filter(table=self.table).count(), 1)

        second = self.seat(2, customer_type='friends', group_name='Group B').json()
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(self.table.seats_free, 0)

        departed = self.client.post(
            reverse('dashboard:server_seating_departed', args=[first['session_id']])
        )
        self.assertEqual(departed.status_code, 200)
        self.assertEqual(self.table.seats_taken, 6)

        cleaned = self.client.post(
            reverse('dashboard:server_seating_cleaned', args=[first['session_id']])
        )
        self.assertEqual(cleaned.status_code, 200)
        self.assertEqual(self.table.seats_taken, 2)
        self.assertEqual(self.table.seats_free, 4)
        self.assertEqual(
            TableSession.objects.get(pk=first['session_id']).status,
            TableSession.STATUS_COMPLETED,
        )

    def test_group_orders_and_cash_billing_are_independent(self):
        category = Category.objects.create(name='Seating test', slug='seating-test')
        food = FoodItem.objects.create(category=category, name='Tea', price='10.00')
        group_a = self.seat(4, customer_type='family').json()
        group_b = self.seat(2, customer_type='couple').json()

        orders = []
        for group in (group_a, group_b):
            response = self.client.post(
                reverse('dashboard:server_take_session_order'),
                json.dumps({
                    'session_id': group['session_id'],
                    'items': [{'food_item_id': food.pk, 'quantity': 1}],
                }),
                content_type='application/json',
            )
            self.assertEqual(response.status_code, 200, response.content)
            orders.append(Order.objects.get(pk=response.json()['order_id']))

        self.assertNotEqual(orders[0].table_session_id, orders[1].table_session_id)
        self.assertEqual(
            TableSession.objects.get(pk=group_a['session_id']).status,
            TableSession.STATUS_ORDERING,
        )
        kitchen = User.objects.create_user('seating-chef', password='x', is_staff=True)
        Profile.objects.create(user=kitchen, role='kitchen')
        self.client.force_login(kitchen)
        self.client.post(
            reverse('dashboard:update_order_status', args=[orders[0].pk, 'preparing']),
            {'eta_minutes': '10'},
        )
        self.assertEqual(
            TableSession.objects.get(pk=group_a['session_id']).status,
            TableSession.STATUS_DINING,
        )

        self.client.force_login(self.server)
        self.client.post(
            reverse('dashboard:server_table_cash_received', args=[orders[0].pk])
        )
        orders[0].refresh_from_db()
        orders[1].refresh_from_db()
        self.assertEqual(orders[0].payment_status, 'paid')
        self.assertEqual(orders[1].payment_status, 'cash_pending')

        detail = self.client.get(
            reverse('dashboard:server_table_billing', args=[self.table.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, group_a['group_id'])
        self.assertContains(detail, group_b['group_id'])
        self.assertContains(detail, 'Family')
        self.assertContains(detail, 'Couple')
        self.assertContains(detail, 'Order #')

    def test_manual_table_orders_create_new_groups_and_enforce_remaining_seats(self):
        category = Category.objects.create(name='Manual seating test', slug='manual-seating-test')
        food = FoodItem.objects.create(category=category, name='Coffee', price='15.00')

        def place_order(guests, customer_type):
            return self.client.post(
                reverse('dashboard:server_take_order'),
                json.dumps({
                    'table_id': self.table.pk,
                    'guests': guests,
                    'customer_type': customer_type,
                    'items': [{'food_item_id': food.pk, 'quantity': 1}],
                }),
                content_type='application/json',
            )

        first = place_order(4, 'family')
        second = place_order(2, 'friends')
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        over_capacity = place_order(1, 'individual')
        self.assertEqual(over_capacity.status_code, 409)

        first_order = Order.objects.get(pk=first.json()['order_id'])
        second_order = Order.objects.get(pk=second.json()['order_id'])
        self.assertNotEqual(first_order.table_session_id, second_order.table_session_id)
        self.assertEqual(self.table.seats_taken, 6)
        self.assertEqual(
            TableSession.objects.get(pk=first_order.table_session_id).customer_type,
            'family',
        )
        self.assertEqual(
            TableSession.objects.get(pk=second_order.table_session_id).customer_type,
            'friends',
        )

    def test_invalid_customer_type_is_rejected(self):
        response = self.seat(1, customer_type='unrelated')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TableSession.objects.count(), 0)

    def test_cancelled_group_is_retained_and_its_seats_are_released(self):
        group = self.seat(2).json()
        response = self.client.post(
            reverse('dashboard:server_seating_cancel', args=[group['session_id']])
        )
        self.assertEqual(response.status_code, 200)
        session = TableSession.objects.get(pk=group['session_id'])
        self.assertEqual(session.status, TableSession.STATUS_CANCELLED)
        self.assertEqual(self.table.seats_free, self.table.capacity)

    def test_individual_bill_menu_uses_take_order_thumbnails_and_fallback(self):
        category = Category.objects.create(name='Thumbnail test', slug='thumbnail-test')
        FoodItem.objects.create(
            category=category,
            name='Dish with photo',
            price='12.00',
            image='food_images/dish.jpg',
        )
        FoodItem.objects.create(
            category=category,
            name='Dish without photo',
            price='10.00',
        )

        response = self.client.get(reverse('dashboard:server_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<img class="to-thumb" src="/media/food_images/dish.jpg"',
            html=False,
        )
        self.assertContains(
            response,
            '<span class="to-ph" data-n="Dish without photo"></span>',
            html=False,
        )
        self.assertContains(
            response,
            "this.style.display='none';this.nextElementSibling.style.display='flex'",
            html=False,
        )

    def test_both_server_menus_render_dynamic_categories_and_item_links(self):
        juice = Category.objects.create(name='Juice', slug='filter-juice')
        biriyani = Category.objects.create(name='Biriyani', slug='filter-biriyani')
        juice_item = FoodItem.objects.create(
            category=juice, name='Apple Juice', price='12.00'
        )
        biriyani_item = FoodItem.objects.create(
            category=biriyani, name='Chicken Biriyani', price='20.00',
            is_veg=False,
        )

        response = self.client.get(reverse('dashboard:server_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context['categories'].values_list('name', flat=True)),
            ['Biriyani', 'Juice'],
        )
        self.assertContains(
            response,
            'class="menu-category-filter active" data-category-id="all" aria-pressed="true">All',
            count=2,
            html=False,
        )
        self.assertContains(
            response,
            'class="veg-toggle-pill" id="toVegOnly" data-veg="true" aria-label="Veg only" aria-pressed="false"',
            html=False,
        )
        self.assertContains(
            response,
            'class="veg-toggle-pill" id="toNonVegOnly" data-veg="false" aria-label="Non-Veg only" aria-pressed="false"',
            html=False,
        )
        self.assertContains(
            response,
            'class="veg-toggle-pill" id="ibVegOnly" data-veg="true" aria-label="Veg only" aria-pressed="false"',
            html=False,
        )
        self.assertContains(
            response,
            'class="veg-toggle-pill" id="ibNonVegOnly" data-veg="false" aria-label="Non-Veg only" aria-pressed="false"',
            html=False,
        )
        for category in (juice, biriyani):
            self.assertContains(
                response,
                f'data-category-id="{category.pk}" aria-pressed="false">{category.name}</button>',
                count=2,
                html=False,
            )
        self.assertContains(response, f'class="to-item" data-id="{juice_item.pk}"', html=False)
        self.assertContains(response, f'class="ib-item" data-id="{juice_item.pk}"', html=False)
        self.assertContains(response, f'class="to-item" data-id="{biriyani_item.pk}"', html=False)
        self.assertContains(response, f'class="ib-item" data-id="{biriyani_item.pk}"', html=False)
        self.assertContains(
            response, f'data-category-id="{juice.pk}" data-veg="true"',
            count=2, html=False,
        )
        self.assertContains(
            response, f'data-category-id="{biriyani.pk}" data-veg="false"',
            count=2, html=False,
        )

    def make_qr_group(self, group_name, with_order=False, refunded=False):
        session = TableSession.objects.create(
            table=self.table,
            guests=3,
            guest_count=3,
            group_name=group_name,
            customer_type=TableSession.CUSTOMER_TYPE_FAMILY,
            source=TableSession.SOURCE_QR,
            status=TableSession.STATUS_ORDERING if with_order else TableSession.STATUS_SEATED,
        )
        order = None
        if with_order:
            order = Order.objects.create(
                table=self.table,
                table_session=session,
                guests=3,
                status='pending',
                payment_method='online',
                payment_status='paid',
                refunded=refunded,
                total_amount='30.00',
            )
        return session, order

    def make_unpaid_qr_group(self, group_name, seats=3, status='pending',
                             payment_status='pending'):
        session = TableSession.objects.create(
            table=self.table,
            guests=seats,
            guest_count=seats,
            group_name=group_name,
            source=TableSession.SOURCE_QR,
            status=TableSession.STATUS_ORDERING,
        )
        order = Order.objects.create(
            table=self.table,
            table_session=session,
            guests=seats,
            status=status,
            payment_method='online',
            payment_status=payment_status,
            total_amount='30.00',
        )
        return session, order

    def table_overview(self):
        response = self.client.get(reverse('dashboard:server_tables_overview'))
        self.assertEqual(response.status_code, 200, response.content)
        return next(row for row in response.json()['tables'] if row['id'] == self.table.pk)

    def test_qr_rejection_and_billing_cancellation_release_only_the_selected_groups(self):
        group_a, order_a = self.make_qr_group(
            'Group A', with_order=True, refunded=True
        )
        group_b, _ = self.make_qr_group('Group B')
        other_table = Table.objects.create(number=76, capacity=2)
        other_group = TableSession.objects.create(
            table=other_table,
            guests=2,
            guest_count=2,
            source=TableSession.SOURCE_QR,
        )

        rejected = self.client.post(
            reverse('dashboard:server_reject_order', args=[order_a.pk]),
            {'reason': 'Unavailable'},
        )
        self.assertEqual(rejected.status_code, 200, rejected.content)
        group_a.refresh_from_db()
        order_a.refresh_from_db()
        self.assertEqual(group_a.status, TableSession.STATUS_CANCELLED)
        self.assertFalse(group_a.is_active)
        self.assertTrue(order_a.refunded)
        self.assertEqual(self.table.seats_free, 3)
        self.assertEqual(self.table_overview()['groups'], 1)
        self.assertEqual(other_table.seats_free, 0)
        self.assertTrue(other_group.is_active)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.server)
        billing = csrf_client.get(
            reverse('dashboard:server_table_billing', args=[self.table.pk])
        )
        self.assertEqual(billing.status_code, 200, billing.content)
        self.assertContains(billing, group_b.group_id)
        self.assertContains(billing, 'Cancel seating')
        csrf_match = re.search(
            r"const csrf = '([^']*)';", billing.content.decode()
        )
        self.assertIsNotNone(csrf_match)
        csrf_token = csrf_match.group(1)
        self.assertTrue(csrf_token)

        cancel_url = reverse(
            'dashboard:server_seating_cancel', args=[group_b.pk]
        )
        cancelled = csrf_client.post(
            cancel_url, HTTP_X_CSRFTOKEN=csrf_token
        )
        repeated = csrf_client.post(
            cancel_url, HTTP_X_CSRFTOKEN=csrf_token
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertTrue(cancelled.json()['ok'])
        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertTrue(repeated.json()['ok'])
        group_b.refresh_from_db()
        self.assertEqual(group_b.status, TableSession.STATUS_CANCELLED)
        self.assertFalse(group_b.is_active)
        self.assertEqual(self.table.seats_free, 6)
        self.assertEqual(self.table_overview()['groups'], 0)
        self.assertEqual(other_table.seats_free, 0)
        billing_after = self.client.get(
            reverse('dashboard:server_table_billing', args=[self.table.pk])
        )
        self.assertEqual(billing_after.context['used_seats'], 0)
        self.assertEqual(billing_after.context['free_seats'], 6)
        self.assertEqual(billing_after.context['group_count'], 0)

        admin = User.objects.create_superuser('seating-admin', password='x')
        self.client.force_login(admin)
        admin_tables = self.client.get(reverse('dashboard:manage_tables'))
        table_row = next(
            row for row in admin_tables.context['rows']
            if row['table'].pk == self.table.pk
        )
        self.assertEqual(table_row['free_seats'], 6)
        self.assertEqual(table_row['active_group_count'], 0)

    @patch('dashboard.views._razorpay_refund', return_value=(False, 'Refund pending'))
    def test_qr_rejection_releases_seats_even_when_refund_is_pending(self, refund):
        group, order = self.make_qr_group('Refund pending', with_order=True)

        response = self.client.post(
            reverse('dashboard:server_reject_order', args=[order.pk])
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['refund'])
        self.assertFalse(response.json()['refunded'])
        refund.assert_called_once()
        group.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(group.status, TableSession.STATUS_CANCELLED)
        self.assertFalse(order.refunded)
        self.assertEqual(self.table.seats_free, self.table.capacity)

    def test_cancel_unpaid_qr_order_releases_only_its_group_and_keeps_history(self):
        group_a, order_a = self.make_unpaid_qr_group('Unpaid A', seats=2)
        group_b, _ = self.make_unpaid_qr_group('Unpaid B', seats=3)
        self.assertEqual(self.table.seats_free, 1)

        response = self.client.post(
            reverse('dashboard:server_cancel_unpaid_order', args=[order_a.pk]),
            {'reason': 'Customer Emergency'},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['ok'])

        order_a.refresh_from_db()
        group_a.refresh_from_db()
        self.assertEqual(order_a.status, 'cancelled')
        self.assertEqual(order_a.payment_status, 'pending')
        self.assertEqual(order_a.cancellation_reason, 'Customer Emergency')
        self.assertIsNotNone(order_a.cancelled_at)
        self.assertEqual(group_a.status, TableSession.STATUS_CANCELLED)
        self.assertEqual(self.table.seats_free, 3)
        self.assertTrue(group_b.is_active)

        repeated = self.client.post(
            reverse('dashboard:server_cancel_unpaid_order', args=[order_a.pk]),
            {'reason': 'Customer Emergency'},
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(self.table.seats_free, 3)

        billing = self.client.get(
            reverse('dashboard:server_table_billing', args=[self.table.pk])
        )
        self.assertEqual(billing.status_code, 200)
        self.assertEqual(billing.context['free_seats'], 3)
        self.assertEqual(billing.context['used_seats'], 3)

        server_page = self.client.get(reverse('dashboard:server_dashboard'))
        self.assertContains(server_page, f'Order #{order_a.pk}')
        self.assertContains(server_page, 'Customer Emergency')
        self.assertContains(server_page, 'Recent Cancellations')

        admin = User.objects.create_superuser('cancel-history-admin', password='x')
        self.client.force_login(admin)
        history = self.client.get(reverse('dashboard:manage_orders'))
        self.assertContains(history, 'Customer Emergency')
        self.assertContains(history, 'Cancelled')
        self.assertContains(history, 'Cancelled:')
        admin_tables = self.client.get(reverse('dashboard:manage_tables'))
        row = next(
            item for item in admin_tables.context['rows']
            if item['table'].pk == self.table.pk
        )
        self.assertEqual(row['free_seats'], 3)

    def test_cancel_unpaid_qr_order_already_in_kitchen_notifies_kitchen(self):
        _, order = self.make_unpaid_qr_group(
            'Kitchen cancellation', status='preparing'
        )

        response = self.client.post(
            reverse('dashboard:server_cancel_unpaid_order', args=[order.pk]),
            {'reason': 'Customer Emergency'},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['kitchen_notified'])
        notification = Notification.objects.get(kind='cancelled', order=order)
        self.assertIn('Customer Emergency', notification.message)
        repeated = self.client.post(
            reverse('dashboard:server_cancel_unpaid_order', args=[order.pk]),
            {'reason': 'Customer Emergency'},
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(Notification.objects.filter(kind='cancelled', order=order).count(), 1)

        chef = User.objects.create_user('cancel-notified-chef', password='x', is_staff=True)
        Profile.objects.create(user=chef, role='kitchen')
        self.client.force_login(chef)
        polled = self.client.get(
            reverse('dashboard:kitchen_notifications_poll')
        )
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.json()['cancellations'][0]['id'], notification.id)
        self.assertIn(
            'Customer Emergency', polled.json()['cancellations'][0]['message']
        )
        kitchen = self.client.get(reverse('dashboard:kitchen_dashboard'))
        self.assertEqual(kitchen.context['confirmed_orders'], [])
        self.assertEqual(kitchen.context['preparing_orders'], [])

    def test_paid_qr_order_cannot_use_unpaid_cancellation(self):
        group, order = self.make_unpaid_qr_group(
            'Paid group', payment_status='paid'
        )

        response = self.client.post(
            reverse('dashboard:server_cancel_unpaid_order', args=[order.pk]),
            {'reason': 'Customer Emergency'},
        )

        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        group.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.cancellation_reason, '')
        self.assertTrue(group.is_active)
        self.assertEqual(self.table.seats_free, 3)

    def test_unpaid_qr_order_appears_with_cancellation_action_on_server_dashboard(self):
        _, order = self.make_unpaid_qr_group('Visible unpaid')

        page = self.client.get(reverse('dashboard:server_dashboard'))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'Order #{order.pk}')
        self.assertContains(page, 'Cancel Unpaid Order &amp; Release Seats')
        poll = self.client.get(reverse('dashboard:server_poll')).json()
        self.assertEqual(
            poll['unpaid_qr_orders'], [{'id': order.pk, 'table': self.table.number}]
        )


class ServerBillAdjustmentTests(TestCase):
    def setUp(self):
        self.server = User.objects.create_user(
            'bill-server', password='x', is_staff=True
        )
        Profile.objects.create(user=self.server, role='server')
        self.client.force_login(self.server)
        category = Category.objects.create(name='Bill test', slug='bill-test')
        self.food = FoodItem.objects.create(
            category=category, name='Dinner', price='4500.00'
        )

    def make_bill(self, total, discount='0.00', coupon=None):
        order = Order.objects.create(
            order_type='takeaway',
            source='server',
            status='confirmed',
            payment_method='cash',
            payment_status='cash_pending',
            discount_amount=discount,
            coupon=coupon,
            total_amount=total,
        )
        OrderItem.objects.create(
            order=order,
            food_item=self.food,
            quantity=1,
            price='4500.00',
        )
        return self.client.get(
            reverse('dashboard:server_table_bill', args=[order.pk])
        )

    def test_bill_separates_tip_and_coupon_discount(self):
        coupon = Coupon.objects.create(
            code='SAVE2',
            discount_percent=2,
            valid_until=timezone.now() + timedelta(days=1),
        )
        response = self.make_bill('4635.00', discount='90.00', coupon=coupon)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tip (5%)')
        self.assertContains(response, '+₹225.00')
        self.assertContains(response, 'Discount')
        self.assertContains(response, '-₹90.00')
        self.assertEqual(response.context['grand_total'], Decimal('4635.00'))
        self.assertEqual(
            response.context['items_total']
            + sum(tip['amount'] for tip in response.context['tip_lines'])
            - response.context['discount_total'],
            response.context['grand_total'],
        )

    def test_bill_labels_ten_percent_tip(self):
        response = self.make_bill('4950.00')

        self.assertContains(response, 'Tip (10%)')
        self.assertContains(response, '+₹450.00')
        self.assertNotContains(
            response, '<div class="t-row"><span>Discount</span>'
        )

    def test_bill_hides_zero_tip_and_discount_lines(self):
        response = self.make_bill('4500.00')

        self.assertEqual(response.context['tip_lines'], [])
        self.assertEqual(response.context['discount_total'], Decimal('0.00'))
        self.assertNotContains(
            response, '<div class="t-row sub"><span>Items total</span>'
        )
        self.assertNotContains(
            response, '<div class="t-row"><span>Discount</span>'
        )


class KitchenToServerToAdminFlowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', password='x')

        self.chef = User.objects.create_user('chef', password='x', is_staff=True)
        Profile.objects.create(user=self.chef, role='kitchen')

        self.waiter = User.objects.create_user('waiter', password='x', is_staff=True)
        Profile.objects.create(user=self.waiter, role='server')

        self.customer = User.objects.create_user('cust', password='x', email='cust@example.com')
        self.table = Table.objects.create(number=5, capacity=4)
        self.order = Order.objects.create(
            user=self.customer, table=self.table, status='confirmed', total_amount=250,
            payment_method='online', payment_status='paid',
        )

    def move(self, user, status):
        self.client.force_login(user)
        return self.client.post(reverse('dashboard:update_order_status', args=[self.order.pk, status]))

    def test_full_flow_kitchen_to_waiter_to_admin(self):
        # kitchen: confirmed -> preparing -> ready
        self.move(self.chef, 'preparing')
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'preparing')
        self.move(self.chef, 'ready')
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'ready')

        # waiter sees it in the ready list (and via the live poll)
        self.client.force_login(self.waiter)
        page = self.client.get(reverse('dashboard:server_dashboard'))
        self.assertContains(page, 'Order #%d' % self.order.pk)
        self.assertContains(page, 'Mark Served')
        poll = self.client.get(reverse('dashboard:server_poll')).json()
        self.assertEqual(poll['orders'], [{'id': self.order.pk, 'table': 5}])

        # waiter serves -> admin gets a message
        self.assertEqual(Notification.objects.count(), 0)
        resp = self.move(self.waiter, 'delivered')
        self.assertRedirects(resp, reverse('dashboard:server_dashboard'), fetch_redirect_response=False)
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'delivered')

        note = Notification.objects.get()
        self.assertIn('Order #%d served to Table 5 by waiter' % self.order.pk, note.message)
        self.assertFalse(note.is_read)
        self.assertEqual(len(mail.outbox), 1)                    # customer email still sent

        # admin sees the message on the dashboard + via poll
        self.client.force_login(self.admin)
        home = self.client.get(reverse('dashboard:admin_home'))
        self.assertContains(home, 'served to Table 5 by waiter')
        data = self.client.get(reverse('dashboard:notifications_poll')).json()
        self.assertEqual(data['unread'], 1)
        self.assertEqual(data['latest_id'], note.id)

        self.client.post(reverse('dashboard:notifications_mark_read'))
        self.assertEqual(self.client.get(reverse('dashboard:notifications_poll')).json()['unread'], 0)

    def test_kitchen_cannot_serve_and_cannot_skip_steps(self):
        self.order.status = 'ready'; self.order.save()
        self.move(self.chef, 'delivered')
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'ready')
        self.assertEqual(Notification.objects.count(), 0)

        self.order.status = 'confirmed'; self.order.save()
        self.move(self.chef, 'ready')                             # skipping "preparing"
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'confirmed')

    def test_server_can_only_serve_ready_orders(self):
        self.move(self.waiter, 'delivered')                       # still "confirmed"
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'confirmed')
        self.move(self.waiter, 'preparing')
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'confirmed')

    def test_status_change_requires_post_and_staff_login(self):
        url = reverse('dashboard:update_order_status', args=[self.order.pk, 'preparing'])
        self.client.force_login(self.chef)
        self.assertEqual(self.client.get(url).status_code, 405)

        # logged-in customer (no staff role) is refused with 403
        self.client.force_login(self.customer)
        self.assertEqual(self.client.post(url).status_code, 403)

        # not logged in at all -> sent to the login page
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)

        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'confirmed')

    def test_invalid_status_is_rejected(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('dashboard:update_order_status', args=[self.order.pk, 'bogus']))
        self.order.refresh_from_db(); self.assertEqual(self.order.status, 'confirmed')

    def test_kitchen_board_has_no_serve_button_and_is_blocked_for_server(self):
        self.order.status = 'ready'; self.order.save()
        self.client.force_login(self.chef)
        page = self.client.get(reverse('dashboard:kitchen_dashboard'))
        self.assertNotContains(page, 'Mark Delivered')
        self.assertContains(page, 'Sent to waiter')

        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse('dashboard:kitchen_dashboard')).status_code, 302)

    def test_admin_notification_endpoints_are_admin_only(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse('dashboard:notifications_poll')).status_code, 302)
        self.client.force_login(self.chef)
        self.assertEqual(self.client.get(reverse('dashboard:server_poll')).status_code, 302)

    def test_sales_do_not_vanish_after_order_is_served(self):
        self.order.status = 'delivered'; self.order.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('dashboard:admin_home')).context['total_sales'], 250)
        self.assertEqual(self.client.get(reverse('dashboard:sales_reports')).context['total_orders'], 1)

    def test_admin_order_dropdown_endpoint(self):
        self.client.force_login(self.admin)
        url = reverse('dashboard:api_order_status', args=[self.order.pk])
        r = self.client.post(url, '{"status": "preparing"}', content_type='application/json')
        # confirmed -> preparing never needs a stock deduction, so stock_used is False
        self.assertEqual(r.json(), {'ok': True, 'status': 'preparing', 'stock_used': False})
        r = self.client.post(url, '{"status": "nope"}', content_type='application/json')
        self.assertEqual(r.status_code, 400)
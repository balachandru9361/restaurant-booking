from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile
from booking.models import Table
from orders.models import Order
from .models import Notification


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
            user=self.customer, table=self.table, status='confirmed', total_amount=250
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
        self.client.force_login(self.customer)
        self.assertEqual(self.client.post(url).status_code, 302)  # sent to login
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
        self.assertEqual(r.json(), {'ok': True, 'status': 'preparing'})
        r = self.client.post(url, '{"status": "nope"}', content_type='application/json')
        self.assertEqual(r.status_code, 400)

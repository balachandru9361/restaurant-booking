from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, FoodItem


class MenuItemDietFormTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='menu-admin',
            email='menu-admin@example.com',
            password='test-password',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Fish', slug='fish')

    def test_add_and_edit_menu_item_can_change_diet(self):
        add_response = self.client.post(
            reverse('dashboard:add_menu_item'),
            {
                'category': self.category.pk,
                'name': 'Nethili 65',
                'description': '',
                'price': '120.00',
                'is_veg': 'False',
                'is_available': 'on',
            },
        )
        self.assertRedirects(add_response, reverse('dashboard:manage_menu'))
        item = FoodItem.objects.get(name='Nethili 65')
        self.assertFalse(item.is_veg)

        edit_url = reverse('dashboard:edit_menu_item', args=[item.pk])
        edit_response = self.client.get(edit_url)
        self.assertEqual(edit_response.status_code, 200)
        self.assertIs(edit_response.context['form']['is_veg'].value(), False)
        self.assertIn('value="False" selected', edit_response.content.decode())
        self.assertEqual(
            edit_response.content.decode().count(
                'ADDITIONAL IMAGES (optional, select 2-3):'
            ),
            1,
        )

        edit_response = self.client.post(
            edit_url,
            {
                'category': self.category.pk,
                'name': item.name,
                'description': '',
                'price': '120.00',
                'is_veg': 'True',
                'is_available': 'on',
            },
        )
        self.assertRedirects(edit_response, reverse('dashboard:manage_menu'))
        item.refresh_from_db()
        self.assertTrue(item.is_veg)

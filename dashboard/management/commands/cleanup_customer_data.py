import csv
import json
import os
from collections import OrderedDict
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import BaseCommand, CommandError, call_command
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Q
from django.db.models.deletion import CASCADE, DO_NOTHING, PROTECT, RESTRICT

from accounts.models import Profile
from booking.models import SeatAllocation, Table, TableBooking, TableSession
from dashboard.models import (
    GroceryItem as DashboardGroceryItem,
    Notification,
    RecipeIngredient as DashboardRecipeIngredient,
    StockRequest,
)
from menu.models import Category, FoodItem, FoodItemImage, Option, OptionGroup
from orders.models import Bill, Coupon, Order, OrderItem
from reviews.models import Review
from store.models import (
    GroceryCategory,
    GroceryItem as StoreGroceryItem,
    RecipeIngredient as StoreRecipeIngredient,
    StockMovement,
)


CUSTOMER_NOTIFICATION_KINDS = ('served', 'rejected', 'ready')
PRESERVED_DATA_MODELS = (
    Category,
    FoodItem,
    FoodItemImage,
    OptionGroup,
    Option,
    Table,
    Coupon,
    DashboardGroceryItem,
    DashboardRecipeIngredient,
    StockRequest,
    GroceryCategory,
    StoreGroceryItem,
    StoreRecipeIngredient,
    StockMovement,
)
SESSION_KEYS_TO_CLEAR = {
    'cart',
    'cart_options',
    'coupon_code',
    'guest_order_ids',
    'is_new_signup',
    'last_booking_id',
    'new_signup_pending',
    'selected_table',
    'table_session_id',
}


class Command(BaseCommand):
    help = (
        'Show a dry-run cleanup report. Deletion requires --confirm and archives '
        'financial data before removal.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--database',
            default=DEFAULT_DB_ALIAS,
            help='Database alias to inspect and clean (default: default).',
        )
        parser.add_argument(
            '--keep-usernames',
            required=True,
            help='Required comma-separated usernames to preserve; must include a superuser.',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Archive data and perform the cleanup. Without this flag nothing is changed.',
        )
        parser.add_argument(
            '--archive-dir',
            help='Archive directory; defaults to BASE_DIR/customer_data_archives.',
        )
        parser.add_argument(
            '--financial-retention-reviewed',
            action='store_true',
            help='Assert the archived financial data meets applicable retention requirements.',
        )

    def handle(self, *args, **options):
        database = options['database']
        keep_usernames = self._parse_keep_usernames(options['keep_usernames'])
        plan = self._collect_plan(database, keep_usernames)
        self._print_report(plan, dry_run=not options['confirm'])

        if not options['confirm']:
            return

        if not options['financial_retention_reviewed']:
            raise CommandError(
                '--financial-retention-reviewed is required with --confirm. '
                'Verify applicable retention requirements before removing financial rows.'
            )

        if plan['preserved_user_fk_blockers']:
            raise CommandError(
                'Cleanup stopped: preserved data has blocking User foreign keys. '
                'Review the blocker records listed above.'
            )

        archive_dir = self._prepare_archive_dir(
            options['archive_dir']
            or Path(settings.BASE_DIR) / 'customer_data_archives'
        )
        stamp = datetime.now(datetime_timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup_path = archive_dir / f'database_backup_{stamp}.json'
        payment_path = archive_dir / f'payments_refunds_{stamp}.csv'
        self._write_database_backup(database, backup_path)
        self._write_payment_archive(database, payment_path)
        self.stdout.write(f'Database backup: {backup_path}')
        self.stdout.write(f'Payment/refund archive: {payment_path}')

        with transaction.atomic(using=database):
            current_plan = self._collect_plan(database, keep_usernames)
            if self._plan_signature(current_plan) != self._plan_signature(plan):
                raise CommandError(
                    'Database counts changed after the report was generated. '
                    'No cleanup was committed; rerun the dry-run during a maintenance window.'
                )
            if current_plan['preserved_user_fk_blockers']:
                raise CommandError(
                    'Cleanup stopped: preserved data has blocking User foreign keys. '
                    'No cleanup was committed.'
                )
            self._apply_cleanup(current_plan, database)

        final_plan = self._collect_plan(database, keep_usernames)
        self.stdout.write(self.style.SUCCESS('Customer data cleanup completed.'))
        self.stdout.write('Final customer-data counts:')
        for model_label, count in final_plan['deletion_counts'].items():
            self.stdout.write(f'  {model_label}: {count}')
        self.stdout.write(
            f'  django.contrib.sessions.Session rows still needing scrub: '
            f'{final_plan["session_scrub_count"]}'
        )
        self.stdout.write(
            f'  orders.Coupon rows with times_used to reset: '
            f'{final_plan["reset_coupon_count"]}'
        )
        self.stdout.write('Final preserved-model counts:')
        for model_label, count in final_plan['preserved_counts'].items():
            self.stdout.write(f'  {model_label}: {count}')

    def _parse_keep_usernames(self, raw):
        names = [name.strip() for name in raw.split(',')]
        if not names or any(not name for name in names):
            raise CommandError('--keep-usernames must be a comma-separated list of non-empty usernames.')
        return list(dict.fromkeys(names))

    def _collect_plan(self, database, keep_usernames):
        user_model = get_user_model()
        all_users = user_model._default_manager.using(database).all()
        kept_users = all_users.filter(username__in=keep_usernames).distinct()
        found_usernames = set(kept_users.values_list('username', flat=True))
        missing_usernames = [name for name in keep_usernames if name not in found_usernames]
        if missing_usernames:
            raise CommandError(
                'These --keep-usernames do not exist: ' + ', '.join(missing_usernames)
            )
        if not kept_users.filter(is_superuser=True).exists():
            raise CommandError('--keep-usernames must include at least one superuser.')

        kept_user_ids = list(kept_users.values_list('pk', flat=True))
        users_to_delete = all_users.exclude(pk__in=kept_user_ids)
        delete_user_ids = list(users_to_delete.values_list('pk', flat=True))
        profiles_to_delete = Profile.objects.using(database).filter(
            user_id__in=delete_user_ids
        )
        deleted_profile_ids = list(profiles_to_delete.values_list('pk', flat=True))

        orders = Order.objects.using(database).all()
        bookings = TableBooking.objects.using(database).all()
        sessions = TableSession.objects.using(database).all()
        allocations = SeatAllocation.objects.using(database).all()
        reviews = Review.objects.using(database).all()
        order_items = OrderItem.objects.using(database).all()
        bills = Bill.objects.using(database).all()

        customer_notifications = Notification.objects.using(database).filter(
            Q(order__isnull=False)
            | Q(kind__in=CUSTOMER_NOTIFICATION_KINDS)
            | Q(created_by_id__in=delete_user_ids)
        )
        notification_ids = list(customer_notifications.values_list('pk', flat=True))

        customer_log_entries = self._customer_log_entries(
            database,
            delete_user_ids,
            deleted_profile_ids,
            notification_ids,
        )
        through_model = TableBooking.extra_tables.through
        booking_table_links = through_model.objects.using(database).all()

        deletion_queries = OrderedDict([
            ('dashboard.Notification (customer/order)', customer_notifications),
            ('django_admin.LogEntry (customer data)', customer_log_entries),
            ('reviews.Review', reviews),
            ('orders.OrderItem', order_items),
            ('orders.Bill', bills),
            ('orders.Order', orders),
            ('booking.TableBooking.extra_tables (through)', booking_table_links),
            ('booking.TableBooking', bookings),
            ('booking.SeatAllocation', allocations),
            ('booking.TableSession', sessions),
            ('accounts.Profile (users to delete)', profiles_to_delete),
            ('auth.User (not in keep list)', users_to_delete),
        ])
        deletion_counts = OrderedDict(
            (label, query.count()) for label, query in deletion_queries.items()
        )

        payment_counts = self._payment_counts(orders)
        session_scrub_count = self._count_sessions_to_scrub(
            database, delete_user_ids
        )
        fk_checks, fk_blockers = self._preserved_user_fk_checks(
            database, delete_user_ids
        )
        preserve_counts = self._preserved_counts(
            database,
            kept_user_ids,
            notification_ids,
            customer_log_entries,
        )
        reset_coupon_count = Coupon.objects.using(database).filter(
            times_used__gt=0
        ).count()

        return {
            'keep_usernames': keep_usernames,
            'kept_user_ids': kept_user_ids,
            'delete_user_ids': delete_user_ids,
            'kept_usernames': list(
                kept_users.order_by('username').values_list('username', flat=True)
            ),
            'deleted_usernames': list(
                users_to_delete.order_by('username').values_list('username', flat=True)
            ),
            'notification_ids': notification_ids,
            'deletion_queries': deletion_queries,
            'deletion_counts': deletion_counts,
            'payment_counts': payment_counts,
            'session_scrub_count': session_scrub_count,
            'preserved_user_fk_checks': fk_checks,
            'preserved_user_fk_blockers': fk_blockers,
            'preserved_counts': preserve_counts,
            'reset_coupon_count': reset_coupon_count,
        }

    def _preserved_user_fk_checks(self, database, user_ids):
        user_model = get_user_model()
        checks = []
        blockers = []
        for model in PRESERVED_DATA_MODELS:
            for field in model._meta.concrete_fields:
                if not (
                    field.is_relation
                    and (field.many_to_one or field.one_to_one)
                    and field.remote_field.model == user_model
                ):
                    continue
                references = list(
                    model._base_manager.using(database)
                    .filter(**{f'{field.name}__in': user_ids})
                    .values_list('pk', flat=True)
                )
                on_delete = field.remote_field.on_delete
                check = {
                    'model': model._meta.label,
                    'field': field.name,
                    'on_delete': getattr(on_delete, '__name__', str(on_delete)),
                    'count': len(references),
                }
                checks.append(check)
                if references and on_delete in (CASCADE, PROTECT, RESTRICT, DO_NOTHING):
                    blockers.append({**check, 'record_ids': references})
        return checks, blockers

    def _customer_log_entries(
        self,
        database,
        customer_user_ids,
        customer_profile_ids,
        notification_ids,
    ):
        model_types = (
            Q(app_label='orders', model__in=('order', 'orderitem', 'bill'))
            | Q(app_label='booking', model__in=('tablebooking', 'tablesession', 'seatallocation'))
            | Q(app_label='reviews', model='review')
        )
        content_type_ids = ContentType.objects.using(database).filter(
            model_types
        ).values_list('pk', flat=True)
        query = Q(content_type_id__in=content_type_ids)
        if customer_user_ids:
            query |= Q(
                content_type__app_label='auth',
                content_type__model='user',
                object_id__in=[str(pk) for pk in customer_user_ids],
            )
        if customer_profile_ids:
            query |= Q(
                content_type__app_label='accounts',
                content_type__model='profile',
                object_id__in=[str(pk) for pk in customer_profile_ids],
            )
        if notification_ids:
            query |= Q(
                content_type__app_label='dashboard',
                content_type__model='notification',
                object_id__in=[str(pk) for pk in notification_ids],
            )
        return LogEntry.objects.using(database).filter(query)

    def _payment_counts(self, orders):
        return OrderedDict([
            ('orders_to_archive', orders.count()),
            ('refunded_orders_to_archive', orders.filter(refund_status='refunded').count()),
            (
                'identifiable_test_mock_ids',
                orders.filter(razorpay_payment_id__startswith='mock_').count(),
            ),
            (
                'provider_ids_mode_unverified',
                orders.exclude(razorpay_payment_id='')
                .exclude(razorpay_payment_id__startswith='mock_')
                .count(),
            ),
            (
                'orders_without_provider_id',
                orders.filter(razorpay_payment_id='').count(),
            ),
        ])

    def _count_sessions_to_scrub(self, database, customer_user_ids):
        customer_user_ids = {str(user_id) for user_id in customer_user_ids}
        count = 0
        for session in Session.objects.using(database).only(
            'session_key', 'session_data'
        ).iterator(chunk_size=500):
            data = session.get_decoded()
            if isinstance(data, dict) and self._customer_session_keys(
                data, customer_user_ids
            ):
                count += 1
        return count

    def _customer_session_keys(self, data, customer_user_ids):
        keys = [
            key for key in data
            if key in SESSION_KEYS_TO_CLEAR or key.startswith('qr_cart_')
        ]
        if str(data.get('_auth_user_id', '')) in customer_user_ids:
            keys.extend(
                key for key in ('_auth_user_id', '_auth_user_backend', '_auth_user_hash')
                if key in data and key not in keys
            )
        return keys

    def _preserved_counts(
        self,
        database,
        kept_user_ids,
        deleted_notification_ids,
        deleted_log_entries,
    ):
        user_model = get_user_model()
        kept_users = user_model._default_manager.using(database).filter(
            pk__in=kept_user_ids
        )
        notification_qs = Notification.objects.using(database).all()
        notification_qs = notification_qs.exclude(pk__in=deleted_notification_ids)
        group_links = user_model.groups.through.objects.using(database).filter(
            user_id__in=kept_user_ids
        )
        permission_links = user_model.user_permissions.through.objects.using(
            database
        ).filter(user_id__in=kept_user_ids)

        preserved = OrderedDict([
            ('auth.User (keep list)', kept_users.count()),
            (
                'accounts.Profile (kept users)',
                Profile.objects.using(database).filter(user_id__in=kept_user_ids).count(),
            ),
            ('auth.User.groups links (kept users)', group_links.count()),
            ('auth.User.user_permissions links (kept users)', permission_links.count()),
            ('auth.Group', apps.get_model('auth', 'Group').objects.using(database).count()),
            ('auth.Permission', apps.get_model('auth', 'Permission').objects.using(database).count()),
            (
                'contenttypes.ContentType',
                ContentType.objects.using(database).count(),
            ),
            ('django.contrib.sessions.Session (all preserved)', Session.objects.using(database).count()),
            ('menu.Category', Category.objects.using(database).count()),
            ('menu.FoodItem', FoodItem.objects.using(database).count()),
            ('menu.FoodItemImage', FoodItemImage.objects.using(database).count()),
            ('menu.OptionGroup', OptionGroup.objects.using(database).count()),
            ('menu.Option', Option.objects.using(database).count()),
            ('booking.Table', Table.objects.using(database).count()),
            ('orders.Coupon (definitions preserved)', Coupon.objects.using(database).count()),
            ('dashboard.Notification (operational)', notification_qs.count()),
            ('dashboard.GroceryItem', DashboardGroceryItem.objects.using(database).count()),
            (
                'dashboard.RecipeIngredient',
                DashboardRecipeIngredient.objects.using(database).count(),
            ),
            ('dashboard.StockRequest', StockRequest.objects.using(database).count()),
            ('store.GroceryCategory', GroceryCategory.objects.using(database).count()),
            ('store.GroceryItem', StoreGroceryItem.objects.using(database).count()),
            (
                'store.RecipeIngredient',
                StoreRecipeIngredient.objects.using(database).count(),
            ),
            ('store.StockMovement (inventory audit)', StockMovement.objects.using(database).count()),
            (
                'django_admin.LogEntry (other)',
                LogEntry.objects.using(database).exclude(
                    pk__in=deleted_log_entries.values_list('pk', flat=True)
                ).count(),
            ),
        ])
        return preserved

    def _plan_signature(self, plan):
        return (
            tuple(plan['kept_user_ids']),
            tuple(plan['delete_user_ids']),
            tuple(plan['kept_usernames']),
            tuple(plan['deleted_usernames']),
            tuple(plan['deletion_counts'].items()),
            tuple(plan['payment_counts'].items()),
            plan['session_scrub_count'],
            tuple(plan['preserved_counts'].items()),
            tuple(
                (item['model'], item['field'], item['on_delete'], item['count'])
                for item in plan['preserved_user_fk_checks']
            ),
            tuple(
                (
                    item['model'],
                    item['field'],
                    item['on_delete'],
                    tuple(item['record_ids']),
                )
                for item in plan['preserved_user_fk_blockers']
            ),
            plan['reset_coupon_count'],
        )

    def _print_report(self, plan, dry_run):
        title = 'DRY RUN — no records changed' if dry_run else 'CONFIRMED CLEANUP PLAN'
        self.stdout.write(title)
        self.stdout.write(
            f'Users to delete ({len(plan["deleted_usernames"])}):'
        )
        for username in plan['deleted_usernames']:
            self.stdout.write(f'  {username!r}')
        self.stdout.write(
            f'Users preserved ({len(plan["kept_usernames"])}):'
        )
        for username in plan['kept_usernames']:
            self.stdout.write(f'  {username!r}')
        self.stdout.write('Records to delete:')
        for model_label, count in plan['deletion_counts'].items():
            self.stdout.write(f'  {model_label}: {count}')
        self.stdout.write(
            f'  django.contrib.sessions.Session rows to scrub (sessions preserved): '
            f'{plan["session_scrub_count"]}'
        )
        self.stdout.write(
            f'  orders.Coupon rows with times_used to reset: {plan["reset_coupon_count"]}'
        )
        self.stdout.write('Payment/refund archive classification (all Order rows):')
        for label, count in plan['payment_counts'].items():
            self.stdout.write(f'  {label}: {count}')
        self.stdout.write(
            '  genuine/test mode for non-mock provider IDs: not stored per transaction; '
            'provider reconciliation required'
        )
        self.stdout.write('Preserved-model counts:')
        for model_label, count in plan['preserved_counts'].items():
            self.stdout.write(f'  {model_label}: {count}')
        self.stdout.write('Preserved-data foreign keys to users slated for deletion:')
        for check in plan['preserved_user_fk_checks']:
            self.stdout.write(
                f'  {check["model"]}.{check["field"]}: {check["count"]} record(s), '
                f'on_delete={check["on_delete"]}'
            )
        if plan['preserved_user_fk_blockers']:
            self.stdout.write('Blocking preserved records (cleanup will abort):')
            for blocker in plan['preserved_user_fk_blockers']:
                self.stdout.write(
                    f'  {blocker["model"]}.{blocker["field"]}: '
                    f'record IDs {blocker["record_ids"]}'
                )
        else:
            self.stdout.write('  No blocking references found.')

    def _prepare_archive_dir(self, raw_path):
        archive_dir = Path(raw_path).expanduser().resolve()
        public_roots = (
            Path(settings.MEDIA_ROOT).resolve() if settings.MEDIA_ROOT else None,
            Path(settings.STATIC_ROOT).resolve() if settings.STATIC_ROOT else None,
        )
        for public_root in public_roots:
            if public_root and (
                archive_dir == public_root or public_root in archive_dir.parents
            ):
                raise CommandError(
                    '--archive-dir must not be inside MEDIA_ROOT or STATIC_ROOT.'
                )
        archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(archive_dir, 0o700)
        return archive_dir

    def _write_database_backup(self, database, path):
        with path.open('x', encoding='utf-8') as output:
            os.chmod(path, 0o600)
            call_command(
                'dumpdata',
                database=database,
                format='json',
                indent=2,
                stdout=output,
            )

    def _write_payment_archive(self, database, path):
        fields = [
            'order_id',
            'created_at',
            'source',
            'order_type',
            'table_id',
            'status',
            'payment_method',
            'payment_status',
            'total_amount',
            'discount_amount',
            'coupon_code',
            'refunded',
            'razorpay_payment_id',
            'transaction_classification',
            'items_json',
        ]
        orders = (
            Order.objects.using(database)
            .select_related('coupon')
            .prefetch_related('items__food_item')
            .order_by('pk')
        )
        with path.open('x', newline='', encoding='utf-8') as output:
            os.chmod(path, 0o600)
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for order in orders.iterator(chunk_size=250):
                items = [
                    {
                        'name': item.food_item.name,
                        'quantity': item.quantity,
                        'unit_price': str(item.price),
                        'subtotal': str(item.subtotal()),
                    }
                    for item in order.items.all()
                ]
                writer.writerow({
                    'order_id': order.pk,
                    'created_at': order.created_at.isoformat(),
                    'source': order.source,
                    'order_type': order.order_type,
                    'table_id': order.table_id or '',
                    'status': order.status,
                    'payment_method': order.payment_method,
                    'payment_status': order.payment_status,
                    'total_amount': str(order.total_amount),
                    'discount_amount': str(order.discount_amount),
                    'coupon_code': order.coupon.code if order.coupon_id else '',
                    'refund_status': order.refund_status,
                    'refunded': order.refund_status == 'refunded',
                    'razorpay_payment_id': order.razorpay_payment_id,
                    'transaction_classification': self._classify_transaction(order),
                    'items_json': json.dumps(items, ensure_ascii=False),
                })

    def _classify_transaction(self, order):
        payment_id = (order.razorpay_payment_id or '').strip()
        if payment_id.startswith('mock_'):
            return 'test_mock_id'
        if payment_id:
            return 'provider_mode_unverified'
        if order.payment_method == 'online' and order.payment_status == 'paid':
            return 'paid_without_provider_id'
        return 'no_provider_id'

    def _apply_cleanup(self, plan, database):
        # Clear only customer cart/booking/order keys; retain every session row
        # and authentication/session-security values for kept users.
        self._scrub_customer_session_data(database, plan['delete_user_ids'])

        for label in (
            'dashboard.Notification (customer/order)',
            'django_admin.LogEntry (customer data)',
            'reviews.Review',
            'orders.OrderItem',
            'orders.Bill',
            'orders.Order',
            'booking.TableBooking.extra_tables (through)',
            'booking.TableBooking',
            'booking.SeatAllocation',
            'booking.TableSession',
            'accounts.Profile (users to delete)',
            'auth.User (not in keep list)',
        ):
            plan['deletion_queries'][label].delete()

        Coupon.objects.using(database).filter(times_used__gt=0).update(times_used=0)

    def _scrub_customer_session_data(self, database, customer_user_ids):
        customer_user_ids = {str(user_id) for user_id in customer_user_ids}
        for session in Session.objects.using(database).only(
            'session_key', 'session_data'
        ).iterator(chunk_size=500):
            data = session.get_decoded()
            if not isinstance(data, dict):
                continue
            keys = self._customer_session_keys(data, customer_user_ids)
            if not keys:
                continue
            for key in keys:
                data.pop(key, None)
            session_data = SessionStore().encode(data)
            Session.objects.using(database).filter(pk=session.pk).update(
                session_data=session_data
            )

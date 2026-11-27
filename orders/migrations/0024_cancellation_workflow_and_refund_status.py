from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_refund_status_forward(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Order.objects.filter(refunded=True).update(refund_status='refunded')


def copy_refund_status_backward(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Order.objects.filter(refund_status='refunded').update(refunded=True)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0023_order_cancelled_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='refund_status',
            field=models.CharField(
                choices=[
                    ('none', 'No refund'),
                    ('pending', 'Refund Pending'),
                    ('partially_refunded', 'Partially Refunded'),
                    ('refunded', 'Refunded'),
                ],
                default='none',
                max_length=20,
            ),
        ),
        migrations.RunPython(copy_refund_status_forward, copy_refund_status_backward),
        migrations.RemoveField(
            model_name='order',
            name='refunded',
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_status',
            field=models.CharField(
                choices=[
                    ('not_requested', 'Not requested'),
                    ('direct', 'Cancelled directly'),
                    ('pending', 'Pending approval'),
                    ('approved', 'Approved'),
                    ('rejected', 'Rejected'),
                ],
                default='not_requested',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_requested_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_decided_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_decided_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='decided_order_cancellations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0021_bill'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='cancellation_reason',
            field=models.CharField(blank=True, max_length=200),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='status',
            field=models.CharField(choices=[('failed', 'Failed'), ('completed', 'Completed'), ('pending', 'Pending'), ('processing', 'Processing'), ('request_sent', 'Request Sent'), ('request_settled', 'Request Settled'), ('request_processing', 'Request Processing')], default='pending', max_length=100),
        ),
        migrations.AlterField(
            model_name='transaction',
            name='transaction_type',
            field=models.CharField(choices=[('transfer', 'Transfer'), ('recieved', 'Recieved'), ('withdraw', 'Withdraw'), ('refund', 'Refund'), ('request', 'Payment Request'), ('none', 'None')], default='none', max_length=100),
        ),
    ]

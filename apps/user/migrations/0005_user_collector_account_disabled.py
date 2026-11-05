from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('user', '0004_collectordisciplinaryrecord_and_more')]

    operations = [
        migrations.AddField(
            model_name='user', name='collector_account_disabled',
            field=models.BooleanField(default=False),
        ),
    ]

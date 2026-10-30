from django.db import migrations, models

from simulator.sql import PROTECTION_SQL


class Migration(migrations.Migration):
    dependencies = [('simulator', '0002_provider_protection')]
    operations = [
        migrations.AddField(
            model_name='providereffect', name='recovery_metadata', field=models.JSONField(default=dict)
        ),
        migrations.RunSQL(
            PROTECTION_SQL.replace(
                'NEW.owner_id, NEW.amount_cents', 'NEW.recovery_metadata, NEW.owner_id, NEW.amount_cents'
            ).replace('OLD.token_id, OLD.owner_id', 'OLD.token_id, OLD.recovery_metadata, OLD.owner_id')
        ),
    ]

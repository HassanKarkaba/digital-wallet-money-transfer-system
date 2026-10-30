from django.db import migrations
from django.db.models import F

def complete_legacy(apps,schema_editor):
    runs=apps.get_model('oversight','ReconciliationRun')
    runs.objects.using(schema_editor.connection.alias).filter(completed_at__isnull=False).update(state='COMPLETED',phase='DONE',cutoff_at=F('created_at'))

class Migration(migrations.Migration):
    dependencies=[('oversight','0004_reconciliationrun_actor_authority_version_and_more')]
    operations=[migrations.RunPython(complete_legacy,migrations.RunPython.noop)]

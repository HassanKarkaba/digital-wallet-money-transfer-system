from django.db import migrations,models
class Migration(migrations.Migration):
    dependencies=[('payments','0001_initial')]
    operations=[migrations.AddField(model_name='replaykey',name='intent_id',field=models.UUIDField(null=True))]

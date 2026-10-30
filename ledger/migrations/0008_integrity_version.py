from django.db import migrations,models
class Migration(migrations.Migration):
    dependencies=[('ledger','0007_commit_invariants')]
    operations=[migrations.AddField(model_name='integritygate',name='version',field=models.PositiveIntegerField(default=1))]

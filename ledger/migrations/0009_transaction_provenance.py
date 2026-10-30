from pathlib import Path
from django.db import migrations,models
class Migration(migrations.Migration):
    dependencies=[('ledger','0008_integrity_version')]
    operations=[migrations.AddField(model_name='journal',name='creation_transaction',field=models.BigIntegerField(default=0)),migrations.RunSQL((Path(__file__).resolve().parents[1]/'sql'/'transaction_provenance.sql').read_text())]

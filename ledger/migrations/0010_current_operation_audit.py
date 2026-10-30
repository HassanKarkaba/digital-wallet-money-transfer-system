from pathlib import Path
from django.db import migrations
class Migration(migrations.Migration):
    dependencies=[('ledger','0009_transaction_provenance')]
    operations=[migrations.RunSQL((Path(__file__).resolve().parents[1]/'sql'/'current_audit.sql').read_text())]

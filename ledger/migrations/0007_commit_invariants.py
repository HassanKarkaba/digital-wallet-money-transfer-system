from pathlib import Path
from django.db import migrations
class Migration(migrations.Migration):
    dependencies=[('ledger','0006_reviewed_recovery')]
    operations=[migrations.RunSQL((Path(__file__).resolve().parents[1]/'sql'/'commit_invariants.sql').read_text())]

from pathlib import Path
from django.db import migrations
class Migration(migrations.Migration):
    dependencies=[('ledger','0003_protected_posting')]
    operations=[migrations.RunSQL((Path(__file__).resolve().parents[1]/'sql'/'admission_locks.sql').read_text())]

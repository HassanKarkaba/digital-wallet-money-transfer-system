from pathlib import Path
from django.db import migrations
class Migration(migrations.Migration):
    dependencies=[('ledger','0004_admission_locks')]
    operations=[migrations.RunSQL((Path(__file__).resolve().parents[1]/'sql'/'immutable_instructions.sql').read_text())]

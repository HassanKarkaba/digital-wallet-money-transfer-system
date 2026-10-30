from pathlib import Path
from django.db import migrations
class Migration(migrations.Migration):
    dependencies=[('ledger','0005_fixed_instructions'),('oversight','0002_recoveryproposal')]
    root=Path(__file__).resolve().parents[1]/'sql'
    operations=[migrations.RunSQL((root/'recovery.sql').read_text()),migrations.RunSQL((root/'recover_operation.sql').read_text())]

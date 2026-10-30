from pathlib import Path
from django.db import migrations

class Migration(migrations.Migration):
    dependencies=[('ledger','0002_initial'),('payments','0001_initial'),('audit','0001_initial')]
    operations=[migrations.RunSQL((Path(__file__).resolve().parents[1]/'sql'/'protection.sql').read_text())]

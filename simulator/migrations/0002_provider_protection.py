from django.db import migrations
from simulator.sql import PROTECTION_SQL
class Migration(migrations.Migration):
    dependencies=[('simulator','0001_initial')]
    operations=[migrations.RunSQL(PROTECTION_SQL)]

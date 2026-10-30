import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Exists, OuterRef


def preserve_legacy_references(apps, schema_editor):
    alias = schema_editor.connection.alias
    if alias != 'default':
        return
    Intent = apps.get_model('payments', 'Intent')
    ReplayKey = apps.get_model('payments', 'ReplayKey')
    Reference = apps.get_model('payments', 'LegacyIntentReference')
    legacy = ReplayKey.objects.using(alias).filter(
        owner_id=OuterRef('owner_id'),
        family=OuterRef('kind'),
        digest=OuterRef('digest'),
        intent_id__isnull=True,
    )
    candidates = (
        Intent.objects.using(alias)
        .filter(consumed_at__isnull=False)
        .annotate(has_legacy_key=Exists(legacy))
        .filter(has_legacy_key=True)
        .values('id', 'owner_id', 'kind', 'digest')
        .order_by('id')
    )
    batch = []
    for row in candidates.iterator(chunk_size=500):
        batch.append(
            Reference(id=row['id'], owner_id=row['owner_id'], family=row['kind'], digest=row['digest'])
        )
        if len(batch) == 500:
            Reference.objects.using(alias).bulk_create(batch)
            batch = []
    if batch:
        Reference.objects.using(alias).bulk_create(batch)


class Migration(migrations.Migration):
    dependencies = [
        ('payments', '0002_retained_intent_reference'),
        ('ledger', '0009_transaction_provenance'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LegacyIntentReference',
            fields=[
                ('id', models.UUIDField(editable=False, primary_key=True, serialize=False)),
                ('family', models.CharField(max_length=24)),
                ('digest', models.CharField(max_length=64)),
                (
                    'owner',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.RunPython(preserve_legacy_references, migrations.RunPython.noop),
        migrations.RunSQL(
            'CREATE TRIGGER immutable_legacy_intent_reference BEFORE UPDATE OR DELETE '
            'ON public.payments_legacyintentreference FOR EACH ROW '
            'EXECUTE FUNCTION public.dwmt_immutable();',
            'DROP TRIGGER immutable_legacy_intent_reference ON public.payments_legacyintentreference;',
        ),
    ]

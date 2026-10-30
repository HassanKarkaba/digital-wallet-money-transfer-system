#!/bin/sh
set -eu
python manage.py shell -c "from django.conf import settings; assert settings.DATABASES['default']['NAME']=='wallet_test' and settings.DATABASES['simulator']['NAME']=='simulator_test', 'Refusing ordinary development databases'"
python manage.py check_readiness --require-all --timeout 120
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --settings=config.test_settings --noinput --keepdb
python scripts/browser_checks.py
python manage.py verify_ledger

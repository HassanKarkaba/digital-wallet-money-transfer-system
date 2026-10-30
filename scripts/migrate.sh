#!/bin/sh
set -eu
export DWMT_DATABASE_ROLE=migration
export DATABASE_URL="$MIGRATION_DATABASE_URL"
export SIMULATOR_DATABASE_URL="$SIMULATOR_MIGRATION_DATABASE_URL"
python manage.py migrate --noinput --database default
python manage.py migrate --noinput --database simulator
python manage.py install_database_permissions
python manage.py initialize_environment

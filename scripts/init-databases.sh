#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
 --set=wallet_password="$WALLET_PASSWORD" --set=sim_password="$SIMULATOR_PASSWORD" \
 --set=migration_password="$MIGRATION_PASSWORD" --set=wallet_db="${WALLET_DB:-wallet}" --set=sim_db="${SIMULATOR_DB:-simulator}" <<'SQL'
CREATE ROLE ledger_owner NOLOGIN;
CREATE ROLE wallet_runtime LOGIN PASSWORD :'wallet_password';
CREATE ROLE simulator_runtime LOGIN PASSWORD :'sim_password';
CREATE ROLE wallet_migrate LOGIN CREATEDB PASSWORD :'migration_password';
CREATE ROLE simulator_migrate LOGIN CREATEDB PASSWORD :'migration_password';
GRANT ledger_owner TO wallet_migrate;
CREATE DATABASE :wallet_db OWNER wallet_migrate;
CREATE DATABASE :sim_db OWNER simulator_migrate;
GRANT CONNECT ON DATABASE :wallet_db TO wallet_runtime;
GRANT CONNECT ON DATABASE :sim_db TO simulator_runtime;
SQL

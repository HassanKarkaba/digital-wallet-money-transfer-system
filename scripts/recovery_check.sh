#!/bin/sh
# Actual physical primary rollback with the independent simulator store retained.
# This owns ONLY the disposable dwmt-verify project. Run separately from verify,
# browser checks and workload measurement, because its exit trap removes that
# project's verification volumes. Ordinary development data is never targeted.
set -eu
umask 077
recovery_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$recovery_root"
if [ "$#" -gt 1 ]; then
    printf '%s\n' 'Usage: scripts/recovery_check.sh [external-results-directory]' >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    mkdir -p -- "$1"
    recovery_output=$(CDPATH= cd -- "$1" && pwd -P)
else
    recovery_output=$(mktemp -d "${TMPDIR:-/tmp}/dwmt-recovery.XXXXXXXX")
fi
case "$recovery_output" in "$recovery_root"|"$recovery_root"/*)
    printf '%s\n' 'Recovery evidence must remain outside the application repository.' >&2
    exit 2;;
esac
compose() { docker compose -p dwmt-verify -f "$recovery_root/compose.test.yaml" "$@"; }
cleanup() {
    recovery_status=$?
    trap - EXIT HUP INT TERM
    # The archive contains only synthetic fixture references/observations. It
    # never includes generated runtime configuration or demo access credentials.
    if ! compose run --rm --no-deps --entrypoint tar tests -C /private -czf - recovery-verification > "$recovery_output/private-observations.tar.gz" 2> "$recovery_output/evidence-copy.log"; then
        if [ "$recovery_status" -eq 0 ]; then recovery_status=1; fi
    fi
    if ! compose down -v --remove-orphans > "$recovery_output/cleanup.log" 2>&1; then
        if [ "$recovery_status" -eq 0 ]; then recovery_status=1; fi
    fi
    printf 'Actual restore check exit=%s. External private evidence: %s\n' "$recovery_status" "$recovery_output"
    exit "$recovery_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# Refuse accidental ambient project targeting: all commands use one literal
# project and definition, and the fixture separately validates both DB names.
compose build
compose down -v --remove-orphans
compose up -d db
compose run --rm migrate
compose stop web worker simulator
compose run --rm --no-deps tests python manage.py recovery_fixture prepare
compose exec -T db pg_dump -U postgres -d wallet_test --format=custom > "$recovery_output/primary-before-payout.dump"
# This payout commits in BOTH stores; only its later primary state is rolled back.
compose run --rm --no-deps tests python manage.py recovery_fixture payout
compose stop web worker simulator
# The marker is outside both restored stores. Advance its expectation before any
# restored application process can admit money under a stale generation.
compose run --rm --no-deps tests python manage.py recovery_fixture mark-expectation --generation 2
compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='wallet_test' AND pid<>pg_backend_pid();" > "$recovery_output/terminated-primary-connections.log"
compose exec -T db dropdb -U postgres --if-exists wallet_test
compose exec -T db createdb -U postgres --owner=wallet_migrate wallet_test
compose exec -T db pg_restore -U postgres --dbname=wallet_test --exit-on-error < "$recovery_output/primary-before-payout.dump"
# Before declared recovery, direct readiness and the admission guard must detect
# the mismatched external marker; reconciliation must find the surviving payout.
compose run --rm --no-deps tests python manage.py recovery_fixture assert-mismatch
compose run --rm --no-deps tests python manage.py recovery_prepare --generation 2
# Only this explicitly isolated invocation uses generation2 after sourcing its
# unchanged test configuration. Original stable identities remain unchanged.
compose run --rm --no-deps -e DWMT_TEST_GENERATION_OVERRIDE=2 tests python manage.py recovery_fixture recover
compose run --rm --no-deps -e DWMT_TEST_GENERATION_OVERRIDE=2 tests python manage.py verify_ledger
printf '%s\n' 'Actual primary restore, retained independent payout and reviewed identity-preserving recovery passed.'

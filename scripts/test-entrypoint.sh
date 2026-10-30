#!/bin/sh
set -eu
set -a
. /test-config/runtime.env
set +a
if [ -n "${DWMT_TEST_GENERATION_OVERRIDE:-}" ]; then
    case "$DATABASE_URL" in */wallet_test) ;; *) printf '%s\n' 'Refusing generation override outside isolated primary database.' >&2; exit 2;; esac
    case "$SIMULATOR_DATABASE_URL" in */simulator_test) ;; *) printf '%s\n' 'Refusing generation override outside isolated simulator database.' >&2; exit 2;; esac
    case "$DWMT_TEST_GENERATION_OVERRIDE" in ''|0*|*[!0-9]*) printf '%s\n' 'Invalid isolated recovery generation.' >&2; exit 2;; esac
    export RECOVERY_GENERATION="$DWMT_TEST_GENERATION_OVERRIDE"
fi
exec "$@"

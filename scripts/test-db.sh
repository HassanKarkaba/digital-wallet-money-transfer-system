#!/bin/sh
set -eu
set -a
. /test-config/runtime.env
set +a
exec docker-entrypoint.sh postgres "$@"

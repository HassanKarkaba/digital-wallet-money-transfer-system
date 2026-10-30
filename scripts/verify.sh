#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
cleanup() { docker compose -p dwmt-verify -f compose.test.yaml down -v --remove-orphans; }
trap cleanup EXIT HUP INT TERM
docker compose -p dwmt-verify -f compose.test.yaml build
docker compose -p dwmt-verify -f compose.test.yaml up -d db
docker compose -p dwmt-verify -f compose.test.yaml run --rm migrate
docker compose -p dwmt-verify -f compose.test.yaml up -d web worker simulator
status=0
docker compose -p dwmt-verify -f compose.test.yaml run --rm tests || status=$?
exit "$status"

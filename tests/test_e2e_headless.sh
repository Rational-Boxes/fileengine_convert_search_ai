#!/bin/bash
# End-to-end: a desktop application drives headless csai with a generated key.
# See tests/e2e_headless.py for what it asserts. Needs the live dev stack.
#
#   FE_USER='testuser@rationalboxes.com' FE_PASS='***' FE_TENANT=default \
#   MAILHOG_URL=http://localhost:18025 ./tests/test_e2e_headless.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${BASE:-http://localhost:8090}"
export BASE IDM="${IDM:-http://localhost:8093}" CSAI="${CSAI:-http://localhost:8092}"
export FE_TENANT="${FE_TENANT:-default}"
if [ -z "${FE_USER:-}" ] || [ -z "${FE_PASS:-}" ]; then
    echo "SKIP: set FE_USER and FE_PASS to run the headless e2e."; exit 0
fi
. "$HERE/../../http_bridge/tests/lib_login.sh"
USER_TOKEN=$(fe_login "$FE_USER" "$FE_PASS" "$FE_TENANT") || { echo "login failed"; exit 1; }
export USER_TOKEN
exec python3 "$HERE/e2e_headless.py"

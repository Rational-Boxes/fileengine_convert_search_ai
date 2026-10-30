#!/bin/bash
# End-to-end: a simulated desktop application drives headless csai.
# See tests/desktop_sim/ (scenarios_live.py, scenarios_sandbox.py). Needs the
# live dev stack; the sandbox tier also starts its own csai, stub model and stub
# MCP server.
#
#   FE_USER='testuser@rationalboxes.com' FE_PASS='***' FE_TENANT=default \
#   MAILHOG_URL=http://localhost:18025 ./tests/test_e2e_desktop.sh [--tier sandbox] [--only consent]
#
# Optional second user for cross-user checks: FE_USER2 / FE_PASS2.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${BASE:-http://localhost:8090}"
export BASE IDM="${IDM:-http://localhost:8093}" CSAI="${CSAI:-http://localhost:8092}"
export FE_TENANT="${FE_TENANT:-default}"
if [ -z "${FE_USER:-}" ] || [ -z "${FE_PASS:-}" ]; then
    echo "SKIP: set FE_USER and FE_PASS to run the desktop e2e."; exit 0
fi
. "$HERE/../../http_bridge/tests/lib_login.sh"
USER_TOKEN=$(fe_login "$FE_USER" "$FE_PASS" "$FE_TENANT") || { echo "login failed"; exit 1; }
export USER_TOKEN
if [ -n "${FE_USER2:-}" ] && [ -n "${FE_PASS2:-}" ]; then
    USER2_TOKEN=$(fe_login "$FE_USER2" "$FE_PASS2" "$FE_TENANT") || { echo "second login failed"; exit 1; }
    export USER2_TOKEN
fi
cd "$HERE/.." && exec python3 -m tests.desktop_sim.run "$@"

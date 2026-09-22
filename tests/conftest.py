# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Shared test fixtures and the ``@live`` gate.

Unit tests run anywhere. Integration tests marked ``live`` need a reachable LDAP
+ gRPC core and are skipped otherwise — the same pattern the MCP server uses, and
credentials come from the environment (no hardcoded creds)."""
import os
import sys

import pytest

# Make the src-layout package importable without an install.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

# Sensible defaults so unit tests are hermetic; real runs override via env/.env.
os.environ.setdefault("FILEENGINE_CSAI_TENANT", "default")

# Take the DATABASE settings from this checkout's .env, which is the file the app
# itself boots from — but nothing else from it.
#
# Without this, `Config()` in a test process fell back to the upstream defaults
# (port 5432, database convert_search_ai, role fileengine_user) while the dev
# Postgres this checkout actually uses is somewhere else entirely. Every DB-backed
# test then skipped with "Postgres (CSAI_PG_*) not reachable" — a true statement
# about a database nobody runs, and indistinguishable from the DB genuinely being
# down. Roughly a dozen tests were dark that way, and two of them had been failing
# for a while behind the skip.
#
# Only CSAI_PG_* keys, and only as defaults: an explicit environment variable
# still wins, so CI can point elsewhere, and the rest of the dev config (chat
# provider, API keys, LDAP) stays out of unit tests, which must not depend on it.
def _seed_db_env_from_dotenv() -> None:
    path = os.path.join(_HERE, "..", ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key.startswith("CSAI_PG_"):
                    os.environ.setdefault(key, value)
    except OSError:
        pass          # no .env is a perfectly normal checkout; defaults apply


_seed_db_env_from_dotenv()


def _live_blocker() -> str:
    """Empty when LDAP + core are reachable and the agent can authenticate;
    otherwise WHY not.

    Two conditions used to share one message. "LDAP/core not reachable" is
    actively misleading when the directory is up and the suite simply has no agent
    credentials to present — it sends you to check infrastructure that is fine.
    test_e2e_live.py and test_auth_coordination_live.py already distinguish the
    two; this now matches them."""
    try:
        from convert_search_ai.config import Config
        from convert_search_ai.ldap_auth import authenticate
        cfg = Config()
        if not cfg.agent_user or not cfg.agent_password:
            return "agent credentials not set (FILEENGINE_CSAI_USER/PASSWORD)"
        if not authenticate(cfg, cfg.agent_user, cfg.agent_password).authenticated:
            return f"agent {cfg.agent_user!r} could not authenticate against {cfg.ldap_endpoint}"
        return ""
    except Exception as e:  # noqa: BLE001 — a gate must not raise
        return f"LDAP/core not reachable ({type(e).__name__}: {e})"


def _db_up() -> bool:
    """True when the configured Postgres is reachable. Connection params come from
    ``Config`` (the ``CSAI_PG_*`` env), so DB-backed tests run against whatever PG
    the environment points at — e.g. ``CSAI_PG_PORT=5434`` for the dev server — and
    are skipped (not hard-failed) when no matching DB is up."""
    try:
        import psycopg

        from convert_search_ai.config import Config
        cfg = Config()
        with psycopg.connect(host=cfg.pg_host, port=cfg.pg_port, dbname=cfg.pg_database,
                             user=cfg.pg_user, password=cfg.pg_password, connect_timeout=2):
            return True
    except Exception:
        return False


_LIVE_BLOCKER = _live_blocker()
live = pytest.mark.skipif(bool(_LIVE_BLOCKER), reason=_LIVE_BLOCKER or "live")
live_db = pytest.mark.skipif(not _db_up(), reason="Postgres (CSAI_PG_*) not reachable")


@pytest.fixture
def config():
    from convert_search_ai.config import Config
    return Config()

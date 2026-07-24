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


def _services_up() -> bool:
    """True when LDAP + core are reachable and the agent can authenticate."""
    try:
        from convert_search_ai.config import Config
        from convert_search_ai.ldap_auth import authenticate
        cfg = Config()
        if not cfg.agent_user or not cfg.agent_password:
            return False
        return authenticate(cfg, cfg.agent_user, cfg.agent_password).authenticated
    except Exception:
        return False


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


live = pytest.mark.skipif(not _services_up(), reason="LDAP/core not reachable")
live_db = pytest.mark.skipif(not _db_up(), reason="Postgres (CSAI_PG_*) not reachable")


@pytest.fixture
def config():
    from convert_search_ai.config import Config
    return Config()

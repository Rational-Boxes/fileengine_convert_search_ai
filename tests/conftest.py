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


live = pytest.mark.skipif(not _services_up(), reason="LDAP/core not reachable")


@pytest.fixture
def config():
    from convert_search_ai.config import Config
    return Config()

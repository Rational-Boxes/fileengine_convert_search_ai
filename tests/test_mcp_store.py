"""McpIntegrationStore against a real Postgres (per-tenant schema). live_db-gated."""
import uuid

import pytest
from conftest import live_db

from convert_search_ai.config import Config
from convert_search_ai.crypto import encrypt_secret, generate_key
from convert_search_ai.mcp_store import McpIntegrationStore

pytestmark = live_db


@pytest.fixture
def store():
    return McpIntegrationStore(Config())


@pytest.fixture
def tenant():
    # A throwaway tenant schema per run so tests don't collide.
    return "mcptest_" + uuid.uuid4().hex[:8]


def test_create_list_get_update_delete(store, tenant):
    key = generate_key()
    integ = store.create(tenant, name="CRM", transport="streamable-http",
                         endpoint_url="https://mcp.example.com/mcp", auth_type="bearer",
                         secret_enc=encrypt_secret(key, "tok"), enabled=True,
                         headers={"X-Env": "prod"}, allowed_tools=["a", "b"],
                         forward_identity=True, created_by="alice")
    assert integ.slug == "crm" and integ.has_secret and integ.forward_identity
    assert integ.headers == {"X-Env": "prod"} and integ.allowed_tools == ["a", "b"]

    assert store.count(tenant) == 1
    assert [i.id for i in store.list(tenant)] == [integ.id]
    assert [i.id for i in store.list(tenant, enabled_only=True)] == [integ.id]

    fetched = store.get(tenant, integ.id)
    assert fetched.name == "CRM" and fetched.auth_type == "bearer"

    # decrypted_secret reads through the ciphertext (never stored in the row view).
    cfg = Config()
    cfg.mcp_secret_key = key
    assert McpIntegrationStore(cfg).decrypted_secret(tenant, integ.id) == "tok"

    updated = store.update(tenant, integ.id, enabled=False, name="CRM Prod")
    assert updated.enabled is False and updated.name == "CRM Prod" and updated.slug == "crm-prod"

    # Clear the allowlist (explicit None) and rotate off the secret.
    cleared = store.update(tenant, integ.id, allowed_tools=None, secret_enc=None)
    assert cleared.allowed_tools is None and cleared.has_secret is False

    assert store.delete(tenant, integ.id) is True
    assert store.get(tenant, integ.id) is None


def test_unique_slug_disambiguates(store, tenant):
    a = store.create(tenant, name="Same Name", transport="sse",
                     endpoint_url="https://a.example/mcp", auth_type="none")
    b = store.create(tenant, name="Same-Name", transport="sse",
                     endpoint_url="https://b.example/mcp", auth_type="none")
    assert a.slug != b.slug and b.slug.startswith("same-name")


def test_allowed_roles_roundtrip(store, tenant):
    integ = store.create(tenant, name="Restricted MCP", transport="streamable-http",
                         endpoint_url="https://mcp.example.com/mcp", auth_type="none",
                         allowed_roles=["engineering", "leadership"], enabled=True)
    assert integ.allowed_roles == ["engineering", "leadership"]
    assert store.get(tenant, integ.id).allowed_roles == ["engineering", "leadership"]
    # narrow the roles, then clear (None = all users)
    assert store.update(tenant, integ.id, allowed_roles=["leadership"]).allowed_roles == ["leadership"]
    assert store.update(tenant, integ.id, allowed_roles=None).allowed_roles is None


def test_oauth_fields_roundtrip(store, tenant):
    key = generate_key()
    integ = store.create(tenant, name="OAuth MCP", transport="streamable-http",
                         endpoint_url="https://mcp.example.com/mcp", auth_type="oauth",
                         secret_enc=encrypt_secret(key, "client-secret"),
                         token_url="https://auth.example.com/token",
                         oauth_client_id="client-1", oauth_scope="mcp.read mcp.write",
                         enabled=True)
    assert integ.auth_type == "oauth" and integ.has_secret
    assert integ.token_url == "https://auth.example.com/token"
    assert integ.oauth_client_id == "client-1" and integ.oauth_scope == "mcp.read mcp.write"
    got = store.get(tenant, integ.id)
    assert got.token_url == integ.token_url and got.oauth_client_id == "client-1"
    # update the scope + rotate client id
    upd = store.update(tenant, integ.id, oauth_scope="mcp.read", oauth_client_id="client-2")
    assert upd.oauth_scope == "mcp.read" and upd.oauth_client_id == "client-2"

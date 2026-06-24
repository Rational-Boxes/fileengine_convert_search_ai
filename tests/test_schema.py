"""Unit tests for per-tenant schema isolation — run anywhere (no DB needed)."""
from convert_search_ai.schema import schema_name, tenant_ddl


def test_default_tenant_maps_to_tenant_default():
    # Mirrors the core's get_schema_prefix: empty/unset -> tenant_default.
    assert schema_name("") == "tenant_default"
    assert schema_name(None) == "tenant_default"
    assert schema_name("default") == "tenant_default"


def test_named_tenant_is_prefixed():
    assert schema_name("acme") == "tenant_acme"


def test_tenant_name_is_sanitized():
    # '-', '.', ' ' (and anything outside [A-Za-z0-9_]) become '_', matching core.
    assert schema_name("tenant-a") == "tenant_tenant_a"
    assert schema_name("a.b c") == "tenant_a_b_c"
    assert schema_name("we;ird") == "tenant_we_ird"


def test_tenant_ddl_targets_the_tenant_schema():
    ddl = tenant_ddl("acme")
    # Tables are created inside the tenant schema (no tenant column).
    assert 'CREATE SCHEMA IF NOT EXISTS "tenant_acme"' in ddl
    assert '"tenant_acme".documents' in ddl
    assert '"tenant_acme".chunks' in ddl
    assert "vector(1024)" in ddl                 # default embedding width
    assert "tenant_default" not in ddl  # isolated to the requested tenant


def test_tenant_ddl_embedding_dimension_is_configurable():
    # The pgvector column width follows CSAI_EMBEDDING_DIMENSION so any model
    # works (e.g. 768 for nomic-embed-text, 1536 for text-embedding-3-small).
    assert "vector(768)" in tenant_ddl("acme", dimension=768)
    assert "vector(1536)" in tenant_ddl("acme", 1536)
    assert "vector(1024)" not in tenant_ddl("acme", 768)

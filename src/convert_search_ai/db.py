"""Postgres access for convert_search_ai — per-tenant schema isolation.

Each tenant's data lives in its own ``tenant_<tenant>`` schema (see schema.py),
mirroring the core. Connections set ``search_path`` to the tenant's schema so the
service's queries are unqualified and naturally scoped to one tenant — the schema
is the tenant boundary.

``psycopg`` is imported lazily so the package imports without it (it is an M1+
runtime dependency; M0 only defines the layer)."""
from __future__ import annotations

from .config import Config
from .schema import ensure_tenant_schema, schema_name


def connect(config: Config):
    """Open a psycopg connection to this service's own database."""
    import psycopg
    return psycopg.connect(config.pg_dsn)


def provision_tenant(config: Config, tenant: str) -> str:
    """Ensure the tenant's schema + tables exist (idempotent). Call on tenant
    onboarding or first event for a tenant. Returns the schema name."""
    conn = connect(config)
    try:
        return ensure_tenant_schema(conn, tenant)
    finally:
        conn.close()


def connect_for_tenant(config: Config, tenant: str, provision: bool = False):
    """A connection whose ``search_path`` is the tenant's schema (then ``public``
    for the extensions). Set ``provision=True`` to create the schema if missing."""
    conn = connect(config)
    name = ensure_tenant_schema(conn, tenant) if provision else schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{name}", public')
        timeout = int(getattr(config, "db_statement_timeout_ms", 0) or 0)
        if timeout > 0:
            # SET does not accept bound params; timeout is an int, so inline it.
            cur.execute(f"SET statement_timeout = {timeout}")
    conn.commit()
    return conn

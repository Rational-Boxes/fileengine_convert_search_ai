"""Postgres access for convert_search_ai — per-tenant schema isolation.

Each tenant's data lives in its own ``tenant_<tenant>`` schema (see schema.py),
mirroring the core. Connections set ``search_path`` to the tenant's schema so the
service's queries are unqualified and naturally scoped to one tenant — the schema
is the tenant boundary.

``psycopg`` is imported lazily so the package imports without it (it is an M1+
runtime dependency; M0 only defines the layer)."""
from __future__ import annotations

from typing import Optional

from .config import Config
from .failover import CircuitBreaker, DegradedReadOnly
from .schema import ensure_tenant_schema, schema_name

# Process-wide breaker tracking the master's availability (REPLICATION_FAILOVER.md).
_breaker: Optional[CircuitBreaker] = None


def _get_breaker(config: Config) -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker(cooldown_s=getattr(config, "failover_cooldown_s", 30))
    return _breaker


def connect(config: Config, readonly: bool = False):
    """Open a psycopg connection to this service's database.

    With no replica configured this connects to the master (unchanged behavior).
    When a replica is configured, the master is the primary for reads + writes;
    if it is unreachable, **reads** fall back to the read-only replica and
    **writes** raise :class:`DegradedReadOnly`. A lazy circuit-breaker re-probes
    the master after a cooldown."""
    import psycopg

    if not getattr(config, "pg_replica_enabled", False):
        return psycopg.connect(config.pg_dsn)

    breaker = _get_breaker(config)
    op_error = getattr(psycopg, "OperationalError", Exception)

    if not readonly:  # WRITE — master only
        if not breaker.should_try_primary():
            raise DegradedReadOnly(
                "primary database unavailable — service is in read-only fallback mode"
            )
        try:
            conn = psycopg.connect(config.pg_dsn)
            breaker.reset()
            return conn
        except op_error as e:
            breaker.trip()
            raise DegradedReadOnly(
                "primary database unavailable — service is in read-only fallback mode"
            ) from e

    # READ — master when available, else the read-only replica.
    if breaker.should_try_primary():
        try:
            conn = psycopg.connect(config.pg_dsn)
            breaker.reset()
            return conn
        except op_error:
            breaker.trip()
    return psycopg.connect(config.pg_replica_dsn)


def provision_tenant(config: Config, tenant: str) -> str:
    """Ensure the tenant's schema + tables exist (idempotent). Call on tenant
    onboarding or first event for a tenant. Returns the schema name."""
    conn = connect(config)
    try:
        return ensure_tenant_schema(conn, tenant, config.embedding_dimension)
    finally:
        conn.close()


# Tenants whose schema has been ensured in this process. Lets read paths bootstrap
# a never-ingested tenant (no UndefinedTable / 500) without re-running the
# idempotent DDL on every single connection.
_provisioned: set[str] = set()


def connect_for_tenant(config: Config, tenant: str, provision: bool = False, readonly: bool = False):
    """A connection whose ``search_path`` is the tenant's schema (then ``public``
    for the extensions). The schema is ensured on the first connection to a tenant
    in this process (and whenever ``provision=True``), so reads never hit a missing
    table on a tenant that hasn't been ingested yet.

    ``readonly=True`` routes reads to the replica during a master outage and **skips
    schema DDL** — a read-only standby can't run it, and replication keeps the schema
    in sync."""
    conn = connect(config, readonly=readonly)
    if not readonly and (provision or tenant not in _provisioned):
        name = ensure_tenant_schema(conn, tenant, config.embedding_dimension)
        _provisioned.add(tenant)
    else:
        name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{name}", public')
        timeout = int(getattr(config, "db_statement_timeout_ms", 0) or 0)
        if timeout > 0:
            # SET does not accept bound params; timeout is an int, so inline it.
            cur.execute(f"SET statement_timeout = {timeout}")
    conn.commit()
    return conn

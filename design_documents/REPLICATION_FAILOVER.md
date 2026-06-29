# convert_search_ai — Postgres & LDAP read-only failover

Status: **Design + implementation** on `feature/replica-failover`.

Part of a workspace-wide feature (see the matching branches in `file_engine_core`,
`http_bridge`, `webdav_bridge`, `mcp`). This document covers the CSAI service,
which touches **both** Postgres (its own DB) and LDAP (auth).

## 1. Goal — disconnect fault tolerance

Deployment topology:

```
        writes + reads                     replication (streaming / syncrepl)
data ─────────────────▶  MASTER (cloud)  ───────────────────────────▶  REPLICA (on-prem, localhost)
                          Postgres + LDAP                                read-only standby
```

The **master** (cloud) is the single primary for all reads and writes. An on-prem
**replica** is kept current by **DB/LDAP-level replication** (Postgres streaming
replication; OpenLDAP syncrepl) — the application never writes to it.

When the cloud master becomes unreachable (connectivity loss), the service enters
**read-only fallback mode**: reads are served from the local replica so the system
stays usable; writes are rejected with a clear degraded error. When the master is
reachable again, normal operation resumes automatically.

## 2. Scope & decisions

- **One active connection for reads *and* writes** — no read/write load splitting.
  Normal operation talks only to the master. The replica is used **only** while the
  master is down.
- **Failover engages only when a replica is configured.** With a single instance
  (no replica) behavior is unchanged — fully backward compatible.
- **Writes hard-fail while degraded** (`DegradedReadOnly`) — they are *not* queued.
- **Detection: a lazy circuit-breaker** (no background threads). A failed primary
  connection trips the breaker for a cooldown; during the cooldown reads go to the
  replica; after it, the next operation re-probes the master and, on success,
  resumes normal operation.
- **Config: `_REPLICA` suffix, opt-in.** The replica host defaults to `localhost`
  when enabled. Failover is off unless a replica is set.

## 3. Configuration (`config.py`)

| Env var | Default | Meaning |
|---------|---------|---------|
| `CSAI_PG_REPLICA_HOST` | _(unset)_ | Replica Postgres host. **Setting it enables PG failover.** |
| `CSAI_PG_REPLICA_ENABLED` | `false` | Alt. enable switch; when true and no host given, host defaults to `localhost`. |
| `CSAI_PG_REPLICA_PORT` | = `CSAI_PG_PORT` | Replica port. |
| `CSAI_PG_REPLICA_DATABASE` | = `CSAI_PG_DATABASE` | Replica db. |
| `CSAI_PG_REPLICA_USER` | = `CSAI_PG_USER` | Replica user. |
| `CSAI_PG_REPLICA_PASSWORD` | = `CSAI_PG_PASSWORD` | Replica password. |
| `FILEENGINE_LDAP_ENDPOINT_REPLICA` | _(unset)_ | Replica LDAP URI. **Setting it enables LDAP failover.** |
| `CSAI_FAILOVER_COOLDOWN_S` | `30` | Circuit-breaker cooldown before re-probing the master. |

## 4. Mechanism

`failover.py`:
- `CircuitBreaker(cooldown_s, clock)` — `should_try_primary()`, `is_degraded()`,
  `trip()`, `reset()`. Pure/clock-injectable (deterministic tests).
- `DegradedReadOnly(RuntimeError)` — raised on writes during an outage.

`db.py` — `connect(config, readonly=False)`:
- **No replica configured** → connect to master (current behavior).
- **Write** (`readonly=False`): master only. Breaker open → raise `DegradedReadOnly`
  immediately; connect error → trip breaker, raise `DegradedReadOnly`.
- **Read** (`readonly=True`): try master if the breaker allows (reset on success);
  on connect error trip the breaker; serve from the **replica** while degraded.
- `connect_for_tenant(..., readonly=False)` threads the flag and **skips DDL**
  (`ensure_tenant_schema`) on read-only connections — the schema exists on the
  replica via replication.

Each DB module passes `readonly=True` from its read methods (see §5); write methods
keep the default. `search.py` is read-only throughout.

`ldap_auth.py` — `authenticate()` tries the master directory first (breaker-gated);
on a connection-level `LDAPException` it trips its breaker and retries the replica.
LDAP auth (bind + group search) is inherently read-only, so the replica serves it
without restriction; there is no write path to gate.

## 5. Read/write classification (CSAI)

- **Reads (`readonly=True`):** `store.get_status`, `vectorstore.ann_search`,
  `search.query` / `get_text`, `conversations.list` / `get` / `owns`.
- **Writes (default):** `store.upsert` / `delete`, `vectorstore.replace` / `delete`,
  `conversations.create` / `append` / `set_title_if_empty` / `delete`,
  `db.provision_tenant`.

## 6. Operational notes

- Indexing/ingest (writes) pauses during a master outage (events redeliver later;
  ingest is idempotent), while search and chat retrieval keep serving from the
  replica. Chat *history writes* fail during the outage; reads of history work.
- The replica must be a real Postgres standby / syncrepl consumer; the app relies
  on it being physically read-only and current.

## 7. Testing

- `CircuitBreaker` state transitions (clock-injected).
- `db.connect` routing: no-replica passthrough; write→master; write-while-degraded
  → `DegradedReadOnly`; read falls back to replica when the master connect fails;
  recovery resets the breaker. (psycopg.connect monkeypatched.)
- `ldap_auth` falls back to the replica URI when the master bind raises a
  connection error.

"""Full-stack ``@live`` integration test (gated).

Exercises convert_search_ai end to end against the **real** stack — gRPC core,
Postgres (pgvector + pg_trgm), Redis, LDAP — covering: event-driven ingest →
pgvector index → permission-gated FTS search → text-request → WebSocket RAG chat
(offline echo provider) → guards → audit → real-time permission-cache invalidation.

Skips fast unless ALL are reachable and configured via the environment:
  FILEENGINE_CSAI_USER / FILEENGINE_CSAI_PASSWORD   (agent LDAP identity → core)
  CSAI_PG_*                                         (the convert_search_ai database)
  FILEENGINE_REDIS_* / REDDIS_*                     (Redis)

Isolation: a unique events stream per run (neither sees nor leaves backlog), and
the consumer paths (ingest worker, cache invalidator) are driven by *synthetic*
events the test publishes — core's real events go to core's own stream, which a
committed test can't control. Core's emission is covered in the core repo + the
manual e2e; here we verify convert_search_ai's consume/convert/index/serve paths.
"""
import json
import tempfile
import time
import uuid

import pytest


def _build_cfg():
    from convert_search_ai.config import Config
    cfg = Config()
    cfg.events_stream = f"csai_test_{uuid.uuid4().hex[:8]}"   # isolate this run
    cfg.chat_provider = "echo"                                # offline, no API key
    cfg.audit_log_file = tempfile.mktemp(prefix="csai_e2e_audit_", suffix=".log")
    return cfg


def _skip_reason(cfg) -> str:
    # Fast path: no creds -> skip without any network.
    if not cfg.agent_user or not cfg.agent_password:
        return "agent credentials not set (FILEENGINE_CSAI_USER/PASSWORD)"
    try:
        from convert_search_ai.core_client import agent_identity
        if not agent_identity(cfg).authenticated:
            return "agent cannot authenticate (LDAP/core)"
    except Exception as e:
        return f"core/LDAP unavailable: {e.__class__.__name__}"
    try:
        from convert_search_ai import db
        db.connect(cfg).close()
    except Exception as e:
        return f"Postgres unavailable: {e.__class__.__name__}"
    try:
        import redis
        redis.Redis(host=cfg.redis_host, port=cfg.redis_port,
                    password=cfg.redis_password or None, db=cfg.redis_db).ping()
    except Exception as e:
        return f"Redis unavailable: {e.__class__.__name__}"
    return ""


_CFG = _build_cfg()
_SKIP = _skip_reason(_CFG)
pytestmark = pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "live")

_CONTENT = (b"# Northern Region Report\n\n| Region | Q1 | Q2 |\n| --- | --- | --- |\n"
            b"| North | 100 | 120 |\n\nRevenue in the northern territory grew strongly this quarter.")


def _publish(cfg, event: dict) -> None:
    import redis
    r = redis.Redis(host=cfg.redis_host, port=cfg.redis_port,
                    password=cfg.redis_password or None, db=cfg.redis_db)
    r.xadd(cfg.events_stream, {"payload": json.dumps(event)})


def _audit_lines(path):
    try:
        return [json.loads(ln[len("audit "):]) for ln in open(path).read().splitlines()
                if ln.startswith("audit ")]
    except OSError:
        return []


@pytest.fixture(scope="module")
def ctx():
    from fastapi.testclient import TestClient
    from convert_search_ai import db
    from convert_search_ai.app import build_app
    from convert_search_ai.core_client import agent_client, agent_identity
    from convert_search_ai.ingest import build_ingestor
    from convert_search_ai.store import DocumentStore

    cfg = _CFG
    db.provision_tenant(cfg, cfg.tenant)

    mf = agent_client(cfg)
    d = mf.mkdir("", f"csai_e2e_{uuid.uuid4().hex[:8]}", tenant=cfg.tenant)
    f = mf.touch(d, "report.md", tenant=cfg.tenant)
    assert mf.put(f, _CONTENT, tenant=cfg.tenant) is not False

    # Event-driven ingest: the worker consumes a (synthetic) file.updated and
    # converts + indexes the file into pgvector.
    ing = build_ingestor(cfg)
    ing.source.ensure_group()
    _publish(cfg, {"type": "file.updated", "tenant": cfg.tenant, "file_uid": f})
    for _ in range(15):
        if ing.run_once(count=16, block_ms=500):
            break

    ident = agent_identity(cfg)
    app = build_app(cfg)                      # configures audit -> cfg.audit_log_file
    client = TestClient(app)
    yield {"cfg": cfg, "mf": mf, "ident": ident, "file": f, "app": app, "client": client}

    mf.remove(f, tenant=cfg.tenant)
    mf.remove(d, tenant=cfg.tenant)
    DocumentStore(cfg).delete(cfg.tenant, f)
    mf.close()
    import os
    try:
        os.remove(cfg.audit_log_file)
    except OSError:
        pass


def _token(ctx):
    r = ctx["client"].post("/auth/token", json={
        "username": ctx["cfg"].agent_user, "password": ctx["cfg"].agent_password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_event_driven_ingest_indexes_into_pgvector(ctx):
    from convert_search_ai import db
    f = ctx["file"]
    conn = db.connect_for_tenant(ctx["cfg"], ctx["cfg"].tenant)
    with conn.cursor() as cur:
        cur.execute("SELECT status, (content_md IS NOT NULL) FROM documents WHERE file_uid=%s", (f,))
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM chunks WHERE file_uid=%s", (f,))
        chunks = cur.fetchone()[0]
    conn.close()
    assert row is not None and row[0] == "indexed" and row[1] is True
    assert chunks > 0


def test_permission_gated_search_and_text(ctx):
    c, f = ctx["client"], ctx["file"]
    H = _token(ctx)
    hits = c.post("/search", json={"query": "northern revenue"}, headers=H).json()["hits"]
    assert f in [h["file_uid"] for h in hits]
    t = c.get(f"/documents/{f}/text", headers=H)
    assert t.status_code == 200 and "Northern Region Report" in t.json()["text"]
    assert c.post("/search", json={"query": "x"}).status_code == 401   # auth required


def test_guard_rejects_oversized_query(ctx):
    H = _token(ctx)
    assert ctx["client"].post("/search", json={"query": "q" * 5000}, headers=H).status_code == 400


def test_rag_chat_streams_and_cites_readable_source(ctx):
    f = ctx["file"]
    token = _token(ctx)["Authorization"].split()[1]
    with ctx["client"].websocket_connect(f"/chat?token={token}") as ws:
        ws.send_json({"message": "What were northern revenues?", "system_prompt": "Be concise."})
        events = []
        while True:
            e = ws.receive_json()
            events.append(e)
            if e["type"] == "done":
                break
    assert any(e["type"] == "token" for e in events)
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    assert any(c["file_uid"] == f for c in cites)


def test_access_is_audited_secret_free(ctx):
    actions = {a["action"] for a in _audit_lines(ctx["cfg"].audit_log_file)}
    assert {"search", "document_text", "chat"} <= actions
    assert all("password" not in json.dumps(a).lower() for a in _audit_lines(ctx["cfg"].audit_log_file))


def test_realtime_permission_cache_invalidation(ctx):
    from convert_search_ai.cache_invalidation import PermissionCacheInvalidator
    from convert_search_ai.events import RedisEventSource

    cfg, ident, f = ctx["cfg"], ctx["ident"], ctx["file"]
    gate = ctx["app"].state.permission_gate
    # Prime the gate by running a search as the user (populates can_read cache).
    ctx["client"].post("/search", json={"query": "northern"}, headers=_token(ctx))
    key = (ident.tenant, ident.user, f)
    assert key in gate._cache

    inv = PermissionCacheInvalidator(
        cfg, gate, source=RedisEventSource(cfg, consumer_name="e2e", group=f"permcache-{uuid.uuid4().hex[:8]}"))
    inv.source.ensure_group()
    _publish(cfg, {"type": "acl.changed", "tenant": cfg.tenant, "file_uid": f, "principal": "someuser"})
    evicted = False
    for _ in range(15):
        inv.run_once(count=64, block_ms=500)
        if key not in gate._cache:
            evicted = True
            break
    assert evicted

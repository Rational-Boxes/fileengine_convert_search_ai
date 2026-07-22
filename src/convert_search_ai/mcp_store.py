"""Per-tenant repository for MCP integration config (MCP_INTEGRATIONS §4).

One row per registered external MCP server, in the tenant's own Postgres schema
(``tenant_<tenant>.mcp_integration``) so isolation is automatic — the schema *is*
the tenant, and every query here runs on a ``search_path``-scoped connection.

The credential is stored **only** Fernet-encrypted (``secret_enc``); this layer
never returns it. Callers get an :class:`McpIntegration` whose ``has_secret`` flag
is the only thing exposed about it. Decryption for an actual tool call goes through
:func:`decrypted_secret`, which reads the ciphertext and hands it to ``crypto``.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import List, Optional

from .config import Config

# url-safe slug: lowercase, alnum + single hyphens. Used as the tool-name namespace
# (``mcp__<slug>__<tool>``) so it must be collision-proof and identifier-clean.
_SLUG_BAD = re.compile(r"[^a-z0-9]+")

# Sentinel for update(): "field not provided" (leave as-is) vs an explicit value
# (including None, which *clears* a nullable column like secret_enc/allowed_tools).
_MISSING = object()


def slugify(name: str) -> str:
    s = _SLUG_BAD.sub("-", (name or "").strip().lower()).strip("-")
    return s or "integration"


@dataclass
class McpIntegration:
    id: str
    name: str
    slug: str
    description: str
    transport: str
    endpoint_url: str
    auth_type: str          # none | bearer | header | oauth
    auth_header: str
    has_secret: bool
    headers: dict
    enabled: bool
    allowed_tools: Optional[List[str]]   # None = all discovered tools
    forward_identity: bool
    token_url: str = ""          # oauth: token endpoint (client-credentials)
    oauth_client_id: str = ""    # oauth: client id
    oauth_scope: str = ""        # oauth: requested scope(s), space-separated
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    def public_dict(self) -> dict:
        """API-safe view — never includes the secret (only ``has_secret``)."""
        return {
            "id": self.id, "name": self.name, "slug": self.slug,
            "description": self.description, "transport": self.transport,
            "endpoint_url": self.endpoint_url, "auth_type": self.auth_type,
            "auth_header": self.auth_header, "has_secret": self.has_secret,
            "headers": self.headers, "enabled": self.enabled,
            "allowed_tools": self.allowed_tools,
            "forward_identity": self.forward_identity,
            "token_url": self.token_url, "oauth_client_id": self.oauth_client_id,
            "oauth_scope": self.oauth_scope,
            "created_by": self.created_by,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


_COLS = ("id, name, slug, description, transport, endpoint_url, auth_type, "
         "auth_header, (secret_enc IS NOT NULL) AS has_secret, headers, enabled, "
         "allowed_tools, forward_identity, token_url, oauth_client_id, oauth_scope, "
         "created_by, created_at::text, updated_at::text")


def _row(r) -> McpIntegration:
    (id_, name, slug, desc, transport, url, auth_type, auth_header, has_secret,
     headers, enabled, allowed, fwd, token_url, oauth_client_id, oauth_scope,
     created_by, created_at, updated_at) = r
    return McpIntegration(
        id=id_, name=name, slug=slug, description=desc or "", transport=transport,
        endpoint_url=url, auth_type=auth_type, auth_header=auth_header or "",
        has_secret=bool(has_secret),
        headers=headers if isinstance(headers, dict) else (json.loads(headers) if headers else {}),
        enabled=bool(enabled),
        allowed_tools=(list(allowed) if isinstance(allowed, list) else
                       (json.loads(allowed) if isinstance(allowed, str) else None)),
        forward_identity=bool(fwd), token_url=token_url or "",
        oauth_client_id=oauth_client_id or "", oauth_scope=oauth_scope or "",
        created_by=created_by or "",
        created_at=created_at or "", updated_at=updated_at or "")


class McpIntegrationStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, provision: bool = False, readonly: bool = False):
        from .db import connect_for_tenant
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    def list(self, tenant: str, *, enabled_only: bool = False) -> List[McpIntegration]:
        where = "WHERE enabled" if enabled_only else ""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM mcp_integration {where} ORDER BY name")
            return [_row(r) for r in cur.fetchall()]

    def get(self, tenant: str, id_: str) -> Optional[McpIntegration]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM mcp_integration WHERE id = %s", (id_,))
            row = cur.fetchone()
            return _row(row) if row else None

    def count(self, tenant: str) -> int:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mcp_integration")
            return int(cur.fetchone()[0])

    def create(self, tenant: str, *, name: str, transport: str, endpoint_url: str,
               auth_type: str, auth_header: str = "", secret_enc: Optional[bytes] = None,
               headers: Optional[dict] = None, allowed_tools: Optional[List[str]] = None,
               enabled: bool = False, forward_identity: bool = False,
               token_url: str = "", oauth_client_id: str = "", oauth_scope: str = "",
               description: str = "", created_by: str = "") -> McpIntegration:
        id_ = uuid.uuid4().hex
        slug = self._unique_slug(tenant, name)
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_integration
                    (id, name, slug, description, transport, endpoint_url, auth_type,
                     auth_header, secret_enc, headers, enabled, allowed_tools,
                     forward_identity, token_url, oauth_client_id, oauth_scope, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s)
                RETURNING """ + _COLS,
                (id_, name, slug, description, transport, endpoint_url, auth_type,
                 auth_header, secret_enc, json.dumps(headers or {}), enabled,
                 (json.dumps(allowed_tools) if allowed_tools is not None else None),
                 forward_identity, token_url, oauth_client_id, oauth_scope, created_by))
            row = cur.fetchone()
            conn.commit()
            return _row(row)

    # Fields a PUT may change. ``secret_enc``/``allowed_tools`` are sentinel-guarded
    # so "not provided" (leave as-is) is distinct from "clear it".
    _UPDATABLE = ("name", "description", "transport", "endpoint_url", "auth_type",
                  "auth_header", "headers", "enabled", "forward_identity",
                  "token_url", "oauth_client_id", "oauth_scope")

    def update(self, tenant: str, id_: str, *, secret_enc=_MISSING,
               allowed_tools=_MISSING, **fields) -> Optional[McpIntegration]:
        sets, params = [], []
        for k in self._UPDATABLE:
            if k in fields and fields[k] is not None:
                v = fields[k]
                sets.append(f"{k} = %s::jsonb" if k == "headers" else f"{k} = %s")
                params.append(json.dumps(v) if k == "headers" else v)
        if "name" in fields and fields["name"]:
            sets.append("slug = %s")
            params.append(self._unique_slug(tenant, fields["name"], exclude_id=id_))
        if secret_enc is not _MISSING:
            sets.append("secret_enc = %s")
            params.append(secret_enc)  # bytes to set/rotate, None to clear
        if allowed_tools is not _MISSING:
            sets.append("allowed_tools = %s::jsonb")
            params.append(json.dumps(allowed_tools) if allowed_tools is not None else None)
        if not sets:
            return self.get(tenant, id_)
        sets.append("updated_at = now()")
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE mcp_integration SET {', '.join(sets)} WHERE id = %s "
                        f"RETURNING {_COLS}", (*params, id_))
            row = cur.fetchone()
            conn.commit()
            return _row(row) if row else None

    def delete(self, tenant: str, id_: str) -> bool:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM mcp_integration WHERE id = %s", (id_,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    def decrypted_secret(self, tenant: str, id_: str) -> Optional[str]:
        """The integration's plaintext credential (for a tool call), or None. The
        ciphertext never leaves this method; callers get only the decrypted value."""
        from .crypto import decrypt_secret
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT secret_enc FROM mcp_integration WHERE id = %s", (id_,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return decrypt_secret(self.config.mcp_secret_key, row[0])

    # -------------------------------------------------------------- helpers
    def _unique_slug(self, tenant: str, name: str, *, exclude_id: str = "") -> str:
        base = slugify(name)
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT slug FROM mcp_integration WHERE id <> %s", (exclude_id,))
            taken = {r[0] for r in cur.fetchall()}
        if base not in taken:
            return base
        for i in range(2, 1000):
            cand = f"{base}-{i}"
            if cand not in taken:
                return cand
        return f"{base}-{uuid.uuid4().hex[:6]}"

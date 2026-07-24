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

"""Tenant-admin management API for MCP integrations (MCP_INTEGRATIONS §5).

All routes live under ``/v1/admin/mcp-integrations`` and are gated by
:func:`_require_admin` — a caller must be a tenant administrator (LDAP
``administrators`` / bridge ``tenant_admin`` / core ``system_admin``). Every query
is scoped to ``identity.tenant`` (per-tenant schema), so one tenant's admin can
never see or touch another's integrations.

Secrets are write-only: a credential is accepted on create/update, Fernet-encrypted
at rest, and NEVER returned (responses carry only ``has_secret``). Writes are
SSRF-checked, reject stdio, and are capped per tenant. Every mutation and every
connection test is audited (content-free — which fields changed, not the secret)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import audit
from ..config import Config
from ..http_auth import extract_tenant, resolve_identity
from ..ldap_auth import Identity

log = logging.getLogger("convert_search_ai.mcp_admin")

router = APIRouter(prefix="/v1/admin/mcp-integrations", tags=["mcp-admin"])

_ADMIN_ROLES = {"administrators", "tenant_admin", "system_admin"}
_TRANSPORTS = {"streamable-http", "sse"}
_AUTH_TYPES = {"none", "bearer", "header", "oauth"}


# ------------------------------- auth gate ---------------------------------
def _resolve(request: Request) -> Identity:
    config: Config = request.app.state.config
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(headers.get("authorization", ""), tenant, config,
                             request.app.state.token_store,
                             getattr(request.app.state, "bridge_verifier", None))
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


def _require_admin(request: Request) -> Identity:
    ident = _resolve(request)
    if not (set(ident.roles) & _ADMIN_ROLES):
        raise HTTPException(status_code=403, detail="tenant administrator required")
    return ident


def _store(request: Request):
    return request.app.state.mcp_store


def _provider(request: Request):
    """The McpToolProvider, if wired — used to bust its discovery cache on change."""
    return getattr(request.app.state, "mcp", None)


def _config(request: Request) -> Config:
    return request.app.state.config


# ------------------------------ validation ---------------------------------
def _validate_write(body: dict, config: Config, *, creating: bool, existing=None) -> dict:
    """Validate + normalize a create/update body. Returns the cleaned fields
    (without the raw secret, which the caller encrypts). Raises HTTPException 400."""
    out: dict = {}

    if "transport" in body or creating:
        transport = str(body.get("transport") or "streamable-http").strip()
        if transport == "stdio":
            raise HTTPException(status_code=400, detail=(
                "stdio transport is not permitted for tenant integrations (it would run "
                "a subprocess on the server); use a streamable-http or sse URL"))
        if transport not in _TRANSPORTS:
            raise HTTPException(status_code=400, detail=f"transport must be one of {sorted(_TRANSPORTS)}")
        out["transport"] = transport

    if "name" in body or creating:
        name = str(body.get("name") or "").strip()
        if creating and not name:
            raise HTTPException(status_code=400, detail="name is required")
        if name:
            out["name"] = name

    if "endpoint_url" in body or creating:
        url = str(body.get("endpoint_url") or "").strip()
        if creating and not url:
            raise HTTPException(status_code=400, detail="endpoint_url is required")
        if url:
            from ..mcp_client import validate_endpoint
            try:
                validate_endpoint(url)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"endpoint_url rejected: {e}")
            out["endpoint_url"] = url

    auth_type = None
    if "auth_type" in body or creating:
        auth_type = str(body.get("auth_type") or "none").strip()
        if auth_type not in _AUTH_TYPES:
            raise HTTPException(status_code=400, detail=f"auth_type must be one of {sorted(_AUTH_TYPES)}")
        out["auth_type"] = auth_type

    if "auth_header" in body:
        out["auth_header"] = str(body.get("auth_header") or "").strip()

    # A header-auth integration needs the header name; default to Authorization.
    eff_auth = auth_type if auth_type is not None else (getattr(existing, "auth_type", "none"))
    if eff_auth == "header" and not (out.get("auth_header") or getattr(existing, "auth_header", "")):
        out["auth_header"] = "Authorization"

    if "description" in body:
        out["description"] = str(body.get("description") or "")

    if "headers" in body:
        h = body.get("headers") or {}
        if not isinstance(h, dict) or any(not isinstance(k, str) for k in h):
            raise HTTPException(status_code=400, detail="headers must be an object of string keys")
        out["headers"] = {str(k): str(v) for k, v in h.items()}

    if "allowed_tools" in body:
        at = body.get("allowed_tools")
        if at is not None and (not isinstance(at, list) or any(not isinstance(x, str) for x in at)):
            raise HTTPException(status_code=400, detail="allowed_tools must be a list of strings or null")
        out["allowed_tools"] = at  # None = all

    if "allowed_roles" in body:
        ar = body.get("allowed_roles")
        if ar is not None and (not isinstance(ar, list) or any(not isinstance(x, str) for x in ar)):
            raise HTTPException(status_code=400, detail="allowed_roles must be a list of strings or null")
        # Normalize: drop blanks; an empty list means "all users" (same as null).
        out["allowed_roles"] = ([r.strip() for r in ar if r and r.strip()] or None) if ar is not None else None

    if "enabled" in body:
        out["enabled"] = bool(body.get("enabled"))

    if "forward_identity" in body:
        out["forward_identity"] = bool(body.get("forward_identity"))

    # OAuth (client-credentials) fields.
    if "token_url" in body or (creating and eff_auth == "oauth"):
        turl = str(body.get("token_url") or "").strip()
        if turl:
            from ..mcp_client import validate_endpoint
            try:
                validate_endpoint(turl)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"token_url rejected: {e}")
        out["token_url"] = turl
    if "oauth_client_id" in body:
        out["oauth_client_id"] = str(body.get("oauth_client_id") or "").strip()
    if "oauth_scope" in body:
        out["oauth_scope"] = str(body.get("oauth_scope") or "").strip()

    # A secret is required to enable bearer/header/oauth auth on create (or when
    # switching to it without one already stored). Store a secret only if a key exists.
    secret = body.get("secret")
    if secret is not None and str(secret) != "":
        if not config.mcp_secret_key:
            raise HTTPException(status_code=400, detail=(
                "cannot store a secret: CSAI_MCP_SECRET_KEY is not configured"))
    if eff_auth in ("bearer", "header", "oauth"):
        has_existing = bool(getattr(existing, "has_secret", False))
        if not (secret or has_existing):
            raise HTTPException(status_code=400, detail=(
                f"auth_type '{eff_auth}' requires a secret"))
    if eff_auth == "oauth":
        eff_turl = out.get("token_url") if "token_url" in out else getattr(existing, "token_url", "")
        eff_cid = out.get("oauth_client_id") if "oauth_client_id" in out else getattr(existing, "oauth_client_id", "")
        if not eff_turl:
            raise HTTPException(status_code=400, detail="auth_type 'oauth' requires token_url")
        if not eff_cid:
            raise HTTPException(status_code=400, detail="auth_type 'oauth' requires oauth_client_id")
    return out


def _changed_fields(clean: dict, *, secret_set: bool) -> list:
    """Content-free list of which fields a write touched (for audit — no values)."""
    fields = [k for k in clean.keys()]
    if secret_set:
        fields.append("secret")
    return fields


# -------------------------------- routes -----------------------------------
@router.get("")
def list_integrations(request: Request, ident: Identity = Depends(_require_admin)) -> dict:
    items = _store(request).list(ident.tenant)
    return {"integrations": [i.public_dict() for i in items]}


@router.post("")
def create_integration(request: Request, body: dict = Body(...),
                       ident: Identity = Depends(_require_admin)) -> dict:
    config, store = _config(request), _store(request)
    if store.count(ident.tenant) >= config.mcp_max_integrations:
        raise HTTPException(status_code=409, detail=(
            f"per-tenant integration cap reached ({config.mcp_max_integrations})"))
    clean = _validate_write(body, config, creating=True)
    secret = body.get("secret")
    secret_enc = None
    if secret:
        from ..crypto import encrypt_secret
        secret_enc = encrypt_secret(config.mcp_secret_key, str(secret))
    integ = store.create(
        ident.tenant, name=clean["name"], transport=clean["transport"],
        endpoint_url=clean["endpoint_url"], auth_type=clean.get("auth_type", "none"),
        auth_header=clean.get("auth_header", ""), secret_enc=secret_enc,
        headers=clean.get("headers"), allowed_tools=clean.get("allowed_tools"),
        enabled=clean.get("enabled", False),
        forward_identity=clean.get("forward_identity", False),
        allowed_roles=clean.get("allowed_roles"),
        token_url=clean.get("token_url", ""),
        oauth_client_id=clean.get("oauth_client_id", ""),
        oauth_scope=clean.get("oauth_scope", ""),
        description=clean.get("description", ""), created_by=ident.user)
    audit.record(action="mcp_admin", user=ident.user, tenant=ident.tenant, result="ok",
                 op="create", integration=integ.slug, fields=_changed_fields(clean, secret_set=bool(secret)))
    _bust(request, ident.tenant, integ.id)
    return integ.public_dict()


@router.get("/{id_}")
def get_integration(id_: str, request: Request, ident: Identity = Depends(_require_admin)) -> dict:
    integ = _store(request).get(ident.tenant, id_)
    if integ is None:
        raise HTTPException(status_code=404, detail="integration not found")
    return integ.public_dict()


@router.put("/{id_}")
def update_integration(id_: str, request: Request, body: dict = Body(...),
                       ident: Identity = Depends(_require_admin)) -> dict:
    config, store = _config(request), _store(request)
    existing = store.get(ident.tenant, id_)
    if existing is None:
        raise HTTPException(status_code=404, detail="integration not found")
    clean = _validate_write(body, config, creating=False, existing=existing)
    kwargs = dict(clean)
    secret = body.get("secret")
    secret_set = False
    if secret is not None:  # present in body ⇒ rotate ("" clears it)
        if secret == "":
            kwargs["secret_enc"] = None
        else:
            from ..crypto import encrypt_secret
            kwargs["secret_enc"] = encrypt_secret(config.mcp_secret_key, str(secret))
        secret_set = True
    if "allowed_tools" in body:
        kwargs["allowed_tools"] = clean.get("allowed_tools")
        clean.pop("allowed_tools", None)  # passed explicitly, not via **fields
    if "allowed_roles" in body:
        kwargs["allowed_roles"] = clean.get("allowed_roles")
        clean.pop("allowed_roles", None)  # sentinel-guarded explicit param in the store
    integ = store.update(ident.tenant, id_, **kwargs)
    audit.record(action="mcp_admin", user=ident.user, tenant=ident.tenant, result="ok",
                 op="update", integration=integ.slug,
                 fields=_changed_fields(clean, secret_set=secret_set))
    _bust(request, ident.tenant, id_)
    return integ.public_dict()


@router.delete("/{id_}")
def delete_integration(id_: str, request: Request, ident: Identity = Depends(_require_admin)) -> dict:
    integ = _store(request).get(ident.tenant, id_)
    if integ is None:
        raise HTTPException(status_code=404, detail="integration not found")
    _store(request).delete(ident.tenant, id_)
    audit.record(action="mcp_admin", user=ident.user, tenant=ident.tenant, result="ok",
                 op="delete", integration=integ.slug)
    _bust(request, ident.tenant, id_)
    return {"deleted": id_}


@router.post("/{id_}/test")
def test_integration(id_: str, request: Request, ident: Identity = Depends(_require_admin)) -> dict:
    """Connect to the stored integration + ``list_tools()`` (no persistence). Powers
    the admin "Test connection" button and the allowlist picker."""
    config, store = _config(request), _store(request)
    integ = store.get(ident.tenant, id_)
    if integ is None:
        raise HTTPException(status_code=404, detail="integration not found")
    from ..mcp_client import _build_headers, discover_tools, McpConnectionError
    headers = _build_headers(config, store, integ, ident)
    audit.record(action="mcp_admin", user=ident.user, tenant=ident.tenant, result="ok",
                 op="test", integration=integ.slug)
    try:
        specs = discover_tools(endpoint_url=integ.endpoint_url, transport=integ.transport,
                               headers=headers,
                               timeout_s=max(1.0, config.mcp_tool_timeout_ms / 1000.0))
    except McpConnectionError as e:
        return {"ok": False, "error": str(e), "tools": []}
    return {"ok": True, "tools": [{"name": s.name, "description": s.description,
                                   "input_schema": s.input_schema} for s in specs]}


@router.post("/test")
def test_config(request: Request, body: dict = Body(...),
                ident: Identity = Depends(_require_admin)) -> dict:
    """Dry-run a not-yet-saved config (used before the first create). Validates the
    endpoint, then connects with the posted plaintext secret — nothing is stored."""
    config = _config(request)
    _validate_write(body, config, creating=True)
    from ..mcp_client import discover_tools, McpConnectionError
    transport = str(body.get("transport") or "streamable-http").strip()
    url = str(body.get("endpoint_url") or "").strip()
    auth_type = str(body.get("auth_type") or "none").strip()
    headers = {str(k): str(v) for k, v in (body.get("headers") or {}).items()}
    secret = body.get("secret")
    if auth_type == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif auth_type == "header" and secret:
        headers[str(body.get("auth_header") or "Authorization")] = str(secret)
    audit.record(action="mcp_admin", user=ident.user, tenant=ident.tenant, result="ok",
                 op="test_config")
    try:
        specs = discover_tools(endpoint_url=url, transport=transport, headers=headers,
                               timeout_s=max(1.0, config.mcp_tool_timeout_ms / 1000.0))
    except McpConnectionError as e:
        return {"ok": False, "error": str(e), "tools": []}
    return {"ok": True, "tools": [{"name": s.name, "description": s.description,
                                   "input_schema": s.input_schema} for s in specs]}


def _bust(request: Request, tenant: str, integ_id: str = "") -> None:
    prov = _provider(request)
    if prov is not None:
        try:
            prov.invalidate(tenant, integ_id)
        except Exception:
            log.debug("MCP cache invalidation failed", exc_info=True)
    # Drop any cached OAuth token for this integration (creds/URL may have changed).
    try:
        from .. import mcp_oauth
        mcp_oauth.invalidate(integ_id)
    except Exception:
        log.debug("MCP oauth cache invalidation failed", exc_info=True)

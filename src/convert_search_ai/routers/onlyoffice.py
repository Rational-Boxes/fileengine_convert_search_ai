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

"""ONLYOFFICE editing endpoints (Phase 1.7 consumer).

Three endpoints, two trust seams (see ``onlyoffice`` module):

* ``GET  /v1/onlyoffice/config/{file_uid}`` — **user-authenticated**. Verifies WRITE
  as the end-user, then returns the signed editor config the SPA hands to
  ``DocsAPI.DocEditor``. The config's document/callback URLs carry **scoped tokens**
  binding this user+file+version.
* ``GET  /v1/onlyoffice/download`` — **token-authenticated** (no session). The
  Document Server fetches the current bytes; CSAI serves them *as the bound user*.
* ``POST /v1/onlyoffice/callback`` — **token-authenticated**. On a save status,
  CSAI verifies the ONLYOFFICE JWT, downloads the edited document, and writes a new
  version *as the bound user* — so the version attributes to the right person.
"""
from __future__ import annotations

import logging
import urllib.request

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import audit, onlyoffice as oo
from ..config import Config
from ..crypto import sign_scoped_token, verify_scoped_token
from ..http_auth import extract_tenant, resolve_identity
from ..ldap_auth import Identity

log = logging.getLogger("convert_search_ai.onlyoffice")

router = APIRouter(prefix="/v1/onlyoffice", tags=["onlyoffice"])

_DOWNLOAD = "oo-download"
_CALLBACK = "oo-callback"


def _cfg(request: Request) -> Config:
    return request.app.state.config


def _enabled_or_404(config: Config):
    if not config.onlyoffice_enabled:
        raise HTTPException(status_code=404, detail="onlyoffice editing disabled")
    if not config.onlyoffice_docserver_url:
        raise HTTPException(status_code=503, detail="CSAI_ONLYOFFICE_DOCSERVER_URL not configured")


def _identity(request: Request) -> Identity:
    config = _cfg(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(headers.get("authorization", ""), tenant, config,
                             request.app.state.token_store,
                             getattr(request.app.state, "bridge_verifier", None))
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


def _client_for(identity: Identity, config: Config):
    from ..core_client import client_for
    return client_for(identity, config)


def _callback_base(request: Request, config: Config) -> str:
    """Where the Document Server reaches CSAI. Configured base wins; else derive from
    the incoming request (dev fallback — only works if the Doc Server shares the host)."""
    return config.onlyoffice_callback_base or str(request.base_url).rstrip("/")


# ------------------------------- config -------------------------------------
@router.get("/config/{file_uid}")
def editor_config(file_uid: str, request: Request) -> dict:
    config = _cfg(request)
    _enabled_or_404(config)
    identity = _identity(request)
    mf = _client_for(identity, config)
    try:
        # Stat (name + version) and WRITE check run as the user → ACL-enforced.
        try:
            info = mf.stat(file_uid)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"file not found: {e}")
        name = getattr(info, "name", "") or file_uid
        version = getattr(info, "version", "") or ""
        dt = oo.document_type_for(name)
        if dt is None:
            raise HTTPException(status_code=415, detail=f"'{name}' is not an editable office document")
        try:
            can_write = mf.check_permission(file_uid, "WRITE")
        except Exception:
            can_write = False
        if not can_write:
            raise HTTPException(status_code=403, detail="you do not have permission to edit this file")
    finally:
        _close(mf)

    doc_type, file_type = dt
    ttl = config.onlyoffice_session_ttl
    secret = config.onlyoffice_signing_secret
    bind = dict(file_uid=file_uid, version=version, user=identity.user,
                tenant=identity.tenant, roles=list(identity.roles))
    dl_token = sign_scoped_token(secret, purpose=_DOWNLOAD, ttl=ttl, **bind)
    cb_token = sign_scoped_token(secret, purpose=_CALLBACK, ttl=ttl, **bind)
    if not dl_token or not cb_token:
        raise HTTPException(status_code=503, detail="onlyoffice signing secret not configured")

    base = _callback_base(request, config)
    cfg = oo.build_editor_config(
        doc_type=doc_type, file_type=file_type, title=name,
        key=oo.document_key(file_uid, version),
        document_url=f"{base}/v1/onlyoffice/download?token={dl_token}",
        callback_url=f"{base}/v1/onlyoffice/callback?token={cb_token}",
        user_id=identity.user, user_name=identity.user,
        jwt_secret=config.onlyoffice_jwt_secret)
    audit.record(action="onlyoffice_open", user=identity.user, tenant=identity.tenant,
                 result="ok", file_uid=file_uid)
    return {"config": cfg, "docserver_url": config.onlyoffice_docserver_url}


# ------------------------------ download ------------------------------------
@router.get("/download")
def download(request: Request, token: str = "") -> Response:
    config = _cfg(request)
    _enabled_or_404(config)
    claims = verify_scoped_token(config.onlyoffice_signing_secret, token, purpose=_DOWNLOAD)
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid or expired download token")
    identity = Identity(user=claims["user"], roles=list(claims.get("roles") or []),
                        tenant=claims.get("tenant", ""), authenticated=True)
    mf = _client_for(identity, config)
    try:
        stream = mf.get(claims["file_uid"])
        data = stream.read() if hasattr(stream, "read") else bytes(stream)
    except Exception as e:
        log.info("onlyoffice download failed for %s: %s", claims.get("file_uid"), e)
        raise HTTPException(status_code=404, detail="document unavailable")
    finally:
        _close(mf)
    audit.record(action="onlyoffice_download", user=identity.user, tenant=identity.tenant,
                 result="ok", file_uid=claims["file_uid"], bytes=len(data))
    return Response(content=data, media_type="application/octet-stream")


# ------------------------------ callback ------------------------------------
@router.post("/callback")
def callback(request: Request, token: str = "", body: dict = Body(default={})) -> JSONResponse:
    config = _cfg(request)
    _enabled_or_404(config)
    claims = verify_scoped_token(config.onlyoffice_signing_secret, token, purpose=_CALLBACK)
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid or expired callback token")

    # Service seam: when the Document Server has JWT enabled, the callback body is a
    # signed envelope ({token: <jwt>}); verify it and use the verified payload.
    payload = body or {}
    if config.onlyoffice_jwt_secret:
        inner = payload.get("token")
        verified = oo.verify_onlyoffice_jwt(config.onlyoffice_jwt_secret, inner) if inner else None
        if verified is None:
            audit.record(action="onlyoffice_callback", user=claims.get("user", ""),
                         tenant=claims.get("tenant", ""), result="denied",
                         file_uid=claims.get("file_uid", ""), reason="bad_jwt")
            raise HTTPException(status_code=401, detail="invalid ONLYOFFICE callback signature")
        payload = verified

    cb = oo.parse_callback(payload)
    if not oo.should_save(cb["status"]):
        return JSONResponse({"error": 0})  # editing/closed-no-change — nothing to persist

    if not cb["url"]:
        return JSONResponse({"error": 0})
    try:
        edited = _fetch(cb["url"], max_bytes=config.onlyoffice_max_bytes)
    except Exception as e:
        log.warning("onlyoffice: could not fetch edited document: %s", e)
        return JSONResponse({"error": 1})

    identity = Identity(user=claims["user"], roles=list(claims.get("roles") or []),
                        tenant=claims.get("tenant", ""), authenticated=True)
    mf = _client_for(identity, config)
    try:
        mf.put(claims["file_uid"], edited)     # new immutable version, as the user
    except Exception as e:
        log.warning("onlyoffice: write-back failed for %s: %s", claims.get("file_uid"), e)
        audit.record(action="onlyoffice_save", user=identity.user, tenant=identity.tenant,
                     result="error", file_uid=claims["file_uid"])
        return JSONResponse({"error": 1})
    finally:
        _close(mf)
    audit.record(action="onlyoffice_save", user=identity.user, tenant=identity.tenant,
                 result="ok", file_uid=claims["file_uid"], bytes=len(edited))
    return JSONResponse({"error": 0})


# --------------------------------- helpers ----------------------------------
def _fetch(url: str, *, max_bytes: int, timeout: float = 30.0) -> bytes:
    """Download the edited document the Document Server produced. The URL is the Doc
    Server's own (trusted, from a JWT-verified callback), so this is a plain fetch."""
    req = urllib.request.Request(url, headers={"User-Agent": "convert-search-ai/onlyoffice"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("edited document exceeds size cap")
    return data


def _close(mf) -> None:
    close = getattr(mf, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass

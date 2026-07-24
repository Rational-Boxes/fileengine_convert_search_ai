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

"""ONLYOFFICE Document Server integration — in-browser editing that writes back
through FileEngine's immutable versioned store (Phase 1.7 consumer).

Trust model (two seams, bound together so saves attribute correctly):

* **User → editor.** The SPA user is already authenticated (bridge token); CSAI
  builds the editor config **server-side** as that user, checking WRITE first. The
  user identity is bound into short-lived **scoped tokens** (``crypto`` module) that
  the Document Server presents back — so the download/callback endpoints, which have
  no browser session, still act *as the impersonated end-user*.
* **Document Server ↔ CSAI.** ONLYOFFICE's own contract is a **JWT-signed** config +
  callback using a shared secret (``CSAI_ONLYOFFICE_JWT_SECRET``). CSAI signs the
  config and verifies the callback with it — the service-to-service seam, distinct
  from the user's session.

This module is pure logic (type mapping, key derivation, config assembly + signing,
callback save decision); the HTTP wiring is in ``routers/onlyoffice``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Optional, Tuple

# extension → (ONLYOFFICE documentType, canonical fileType). Editable office formats
# only; everything else falls through to view/no-edit. HTML is edited graphically in
# the word editor and round-trips as HTML (the Document Server accepts html as both
# an input and an output format) — so a stored .html (e.g. an AI-generated report)
# can be edited WYSIWYG and saved back as HTML.
_WORD = ("doc", "docx", "docm", "dot", "dotx", "odt", "ott", "rtf", "txt", "html", "htm")
_CELL = ("xls", "xlsx", "xlsm", "xlt", "xltx", "ods", "ots", "csv")
_SLIDE = ("ppt", "pptx", "pptm", "pot", "potx", "odp", "otp")

_TYPE_MAP = {**{e: ("word", e) for e in _WORD},
             **{e: ("cell", e) for e in _CELL},
             **{e: ("slide", e) for e in _SLIDE}}


def _ext(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def document_type_for(name: str) -> Optional[Tuple[str, str]]:
    """``(documentType, fileType)`` for an editable office file, or ``None``.

    documentType is ONLYOFFICE's editor family (``word``/``cell``/``slide``);
    fileType is the file extension the Document Server loads."""
    return _TYPE_MAP.get(_ext(name))


def is_editable(name: str) -> bool:
    return document_type_for(name) is not None


_KEY_SAFE = re.compile(r"[^0-9A-Za-z_-]")


def document_key(file_uid: str, version: str) -> str:
    """A per-(file, version) key ONLYOFFICE uses to cache/co-edit a document. It
    **must change when the content changes** so the Document Server reloads the new
    version; ≤128 chars, ``[0-9A-Za-z_-]`` only (ONLYOFFICE constraint)."""
    raw = f"{file_uid}:{version}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    prefix = _KEY_SAFE.sub("-", file_uid)[:20]
    return f"{prefix}-{digest}"[:128]


# ONLYOFFICE callback status codes (Document Server → host):
#   1 = document being edited            2 = ready to save (all users left)
#   3 = save error                       4 = closed with no changes
#   6 = force-save (still being edited)   7 = force-save error
_SAVE_STATUSES = (2, 6)


def should_save(status: int) -> bool:
    """True when a callback status means "persist the edited document now"."""
    return int(status) in _SAVE_STATUSES


# --------------------------- ONLYOFFICE JWT (service seam) -------------------
def _b64url(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_onlyoffice_jwt(secret: str, payload: dict) -> str:
    """HS256-sign a payload the way the ONLYOFFICE Document Server expects (the
    config ``token`` and callback signatures use this shared-secret JWT)."""
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def verify_onlyoffice_jwt(secret: str, token: str) -> Optional[dict]:
    """Verify an ONLYOFFICE-signed JWT (callback body ``token`` / Authorization
    header). Returns the claims or ``None``. No ``exp`` requirement (ONLYOFFICE
    callback tokens are not always time-bound); signature is the trust anchor."""
    from .jwt_verify import verify_hs256
    if not secret or not token:
        return None
    # ONLYOFFICE tokens may omit exp; verify_hs256 tolerates a missing exp.
    return verify_hs256(token, secret)


def build_editor_config(*, doc_type: str, file_type: str, title: str, key: str,
                        document_url: str, callback_url: str, user_id: str,
                        user_name: str, mode: str = "edit",
                        jwt_secret: str = "") -> dict:
    """Assemble the ONLYOFFICE editor config the SPA hands to ``DocsAPI.DocEditor``.
    When ``jwt_secret`` is set, the config is signed into ``config['token']`` (the
    Document Server rejects an unsigned/forged config when JWT is enabled)."""
    config = {
        "documentType": doc_type,
        "document": {
            "fileType": file_type,
            "key": key,
            "title": title,
            "url": document_url,
            "permissions": {"edit": mode == "edit", "download": True},
        },
        "editorConfig": {
            "mode": mode,
            "callbackUrl": callback_url,
            "user": {"id": user_id, "name": user_name or user_id},
            # autosave + forcesave: the editor saves automatically (a callback on
            # close writes a version), AND the Save button is active so the user can
            # force an on-demand version (a status-6 forcesave callback). Without
            # forcesave the Save icon greys out because saving is automatic.
            "customization": {"autosave": True, "forcesave": True},
        },
    }
    if jwt_secret:
        config["token"] = sign_onlyoffice_jwt(jwt_secret, config)
    return config


def parse_callback(body: dict) -> dict:
    """Normalize the fields we use from an ONLYOFFICE save callback: ``status``,
    the edited-document ``url`` (present on save), and ``key``."""
    return {
        "status": int(body.get("status", 0) or 0),
        "url": str(body.get("url", "") or ""),
        "key": str(body.get("key", "") or ""),
    }

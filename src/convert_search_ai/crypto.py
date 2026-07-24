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

"""Secret encryption + minimal identity signing for MCP integrations.

Two independent concerns, both keyed off deployment config:

  * ``encrypt_secret`` / ``decrypt_secret`` — Fernet (AES-128-CBC + HMAC) at-rest
    encryption of an integration's stored credential, keyed by ``CSAI_MCP_SECRET_KEY``.
    Mirrors ldap_manager's TOTP-secret encryption. Secrets are only ever stored
    encrypted and are never returned by the admin API (write-only).

  * ``sign_identity_assertion`` — a compact HS256 JWT carrying ONLY a minimal
    end-user claim (stable id + tenant), sent to an integration whose
    ``forward_identity`` is enabled (opt-in, MCP_INTEGRATIONS §7) so the MCP server
    can authorize per-user. It never carries roles, core tokens, or ACLs, and is
    short-lived. Signed with ``CSAI_MCP_IDENTITY_SECRET`` (defaults to the shared
    ``FILEENGINE_JWT_SECRET``), verifiable with the same primitive the bridge uses.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional


class SecretError(RuntimeError):
    """A configuration/crypto failure encrypting or decrypting a secret."""


def _fernet(secret_key: str):
    from cryptography.fernet import Fernet
    if not secret_key:
        raise SecretError(
            "CSAI_MCP_SECRET_KEY is not set; cannot encrypt/decrypt MCP secrets")
    key = secret_key.encode() if isinstance(secret_key, str) else secret_key
    try:
        return Fernet(key)
    except Exception as e:  # malformed key (not a urlsafe 32-byte base64)
        raise SecretError(f"CSAI_MCP_SECRET_KEY is invalid: {e}") from e


def encrypt_secret(secret_key: str, plaintext: str) -> bytes:
    """Fernet-encrypt a credential for at-rest storage (``bytea``)."""
    return _fernet(secret_key).encrypt((plaintext or "").encode("utf-8"))


def decrypt_secret(secret_key: str, blob: bytes) -> str:
    """Decrypt a stored credential. Raises :class:`SecretError` if the key is wrong
    or the blob is corrupt (never leaks the ciphertext)."""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken as e:
        raise SecretError("could not decrypt MCP secret (wrong key or corrupt data)") from e


def generate_key() -> str:
    """A fresh urlsafe base64 Fernet key (operator convenience / tests)."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("ascii")


# --------------------------- identity assertion ------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_identity_assertion(secret: str, *, user: str, tenant: str,
                            ttl: int = 120) -> Optional[str]:
    """A short-lived HS256 JWT carrying the MINIMAL user claim (sub + tenant) for a
    ``forward_identity`` integration. Returns ``None`` when no secret is configured
    (so the caller simply forwards nothing). No roles/tokens/ACLs are ever included."""
    if not secret:
        return None
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"sub": user or "", "tenant": tenant or "", "iat": now,
              "exp": now + max(1, int(ttl)), "src": "convert_search_ai"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("ascii"),
                   hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


# ------------------------- scoped service tokens -----------------------------
# Short-lived HS256 tokens CSAI issues *and* verifies itself, to authorize a
# trusted peer service (the ONLYOFFICE Document Server) to fetch/write a specific
# file+version on behalf of a bound end-user — without a browser session. The
# ``purpose`` claim pins each token to one operation (a download token can't be
# replayed as a callback token) and the identity claims carry the impersonation
# subject so the write attributes to the right user.
def sign_scoped_token(secret: str, *, purpose: str, ttl: int, **claims) -> Optional[str]:
    """Issue a scoped HS256 token: ``purpose`` + ``exp`` + arbitrary ``claims``
    (e.g. file_uid, version, user, roles, tenant). Returns None with no secret."""
    if not secret:
        return None
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {**claims, "purpose": purpose, "iat": now,
               "exp": now + max(1, int(ttl)), "src": "convert_search_ai"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("ascii"),
                   hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def verify_scoped_token(secret: str, token: str, *, purpose: str) -> Optional[dict]:
    """Verify signature + ``exp`` + that ``purpose`` matches. Returns the claims or
    ``None``. Reuses the constant-time HS256 verifier."""
    from .jwt_verify import verify_hs256
    if not secret or not token:
        return None
    claims = verify_hs256(token, secret)
    if claims is None or claims.get("purpose") != purpose:
        return None
    return claims

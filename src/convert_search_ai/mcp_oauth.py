"""OAuth 2.0 client-credentials token acquisition for MCP integrations.

Some MCP servers sit behind OAuth (RFC 6749 §4.4 client-credentials): a tenant admin
configures a ``token_url`` + ``oauth_client_id`` + a client secret, and CSAI — acting
as the OAuth **client** — exchanges them for a short-lived bearer token, which it then
presents to the MCP server. Tokens are cached per integration and refreshed shortly
before expiry, so a chatty tool loop doesn't hammer the token endpoint.

This is the "advanced MCP integrations" seam: FileEngine authenticating *to* an
external OAuth-protected server (distinct from FileEngine's own OAuth authority in
ldap_manager, which is FileEngine authenticating *others*).
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple


class McpOAuthError(Exception):
    """A failure obtaining an OAuth token for an MCP integration."""


# Refresh this many seconds before the token actually expires (clock skew + latency).
_REFRESH_SKEW = 30


def fetch_client_credentials_token(
    token_url: str, client_id: str, client_secret: str, scope: str = "",
    *, timeout_s: float = 15.0) -> Tuple[str, int]:
    """Exchange client credentials for ``(access_token, expires_in_seconds)``.

    Client authentication is HTTP Basic (the widely-supported default); the grant is
    form-encoded. Raises :class:`McpOAuthError` on any failure. The token endpoint is
    validated (https + public host) to block SSRF, mirroring the endpoint guard."""
    from .mcp_client import validate_endpoint
    try:
        validate_endpoint(token_url)
    except ValueError as e:
        raise McpOAuthError(f"token_url rejected: {e}") from e

    form = {"grant_type": "client_credentials"}
    if scope:
        form["scope"] = scope
    body = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(token_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", "Basic " + basic)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise McpOAuthError(f"token endpoint returned HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise McpOAuthError(f"token endpoint unreachable: {e}") from e
    token = payload.get("access_token")
    if not token:
        raise McpOAuthError("token endpoint response had no access_token")
    try:
        expires_in = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    return str(token), expires_in


class OAuthTokenCache:
    """Process-wide cache of client-credentials tokens, keyed by the integration and
    its credential-defining fields (so a config change invalidates the cache)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict = {}   # key -> (token, expiry_epoch)

    @staticmethod
    def _key(integ, secret: str) -> tuple:
        # Include the secret hash so rotating the client secret busts the cache; not
        # the raw secret (never store it in a key). token_url/client_id/scope too.
        import hashlib
        sh = hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:16]
        return (integ.id, integ.token_url, integ.oauth_client_id, integ.oauth_scope, sh)

    def get_token(self, integ, secret: str, *, timeout_s: float = 15.0,
                  fetch: Optional[Callable] = None, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        key = self._key(integ, secret)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now + _REFRESH_SKEW:
                return hit[0]
        token, expires_in = (fetch or fetch_client_credentials_token)(
            integ.token_url, integ.oauth_client_id, secret, integ.oauth_scope,
            timeout_s=timeout_s)
        with self._lock:
            self._cache[key] = (token, now + max(0, expires_in))
        return token

    def invalidate(self, integ_id: str = "") -> None:
        with self._lock:
            for k in [k for k in self._cache if not integ_id or k[0] == integ_id]:
                self._cache.pop(k, None)


# Module-level singleton the header builder uses.
_CACHE = OAuthTokenCache()


def get_access_token(integ, secret: str, *, timeout_s: float = 15.0) -> str:
    """Cached client-credentials access token for ``integ`` (fetches/refreshes as
    needed). Raises :class:`McpOAuthError` on failure."""
    return _CACHE.get_token(integ, secret, timeout_s=timeout_s)


def invalidate(integ_id: str = "") -> None:
    _CACHE.invalidate(integ_id)

"""Accept bridge-issued bearer tokens by introspecting them against the core
REST API (http_bridge), so one login authenticates across both services.

The bridge is the upstream token authority: a token it minted (via LDAP or
OAuth) is validated here by calling its RFC 7662-style ``GET /v1/auth/introspect``,
which returns the resolved identity (user, tenant, roles) for a valid token and
401 for an invalid one. Results are cached briefly (``CSAI_BRIDGE_INTROSPECT_TTL``)
so this is not a per-request round-trip. No shared secret or common token format
is needed — only the bridge's URL — keeping the two services loosely coupled.

The request tenant (``X-Tenant``) is forwarded so the bridge resolves the
identity in the same tenant the caller is operating in; the returned identity is
already tenant-scoped, so callers use it as-is."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .ldap_auth import Identity
from .jwt_verify import identity_from_claims, verify_hs256


class BridgeTokenVerifier:
    """Validates http_bridge bearer tokens via the bridge's introspection
    endpoint, with a small TTL cache. Disabled (always returns ``None``) when no
    bridge URL is configured."""

    def __init__(self, base_url: str, ttl_seconds: int = 60, timeout: float = 3.0,
                 jwt_secret: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.ttl = ttl_seconds
        self.timeout = timeout
        self.jwt_secret = jwt_secret or ""
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[Identity, float]] = {}

    @property
    def enabled(self) -> bool:
        # Verifiable if we can check signatures locally OR reach the bridge.
        return bool(self.jwt_secret) or bool(self.base_url)

    def verify(self, token: str, tenant: str) -> Optional[Identity]:
        """Resolve a bridge bearer ``token`` to an Identity scoped to ``tenant``,
        or ``None`` if coordination is off, the token is empty/invalid, or the
        bridge rejects it. When a shared JWT secret is configured the signed token
        is verified LOCALLY (no round-trip); otherwise introspection is used."""
        if not token or not self.enabled:
            return None
        # Local HS256 verification of the bridge's signed JWT.
        if self.jwt_secret:
            claims = verify_hs256(token, self.jwt_secret)
            if claims is None:
                return None  # invalid/expired — do not fall back to introspection
            got = identity_from_claims(claims, tenant)
            if got is None:
                return None
            user, roles = got
            return Identity(user=user, roles=roles,
                            tenant=tenant or claims.get("tenant", "default"),
                            authenticated=True)
        # No shared secret: fall back to bridge introspection (cached).
        key = (token, tenant)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        ident = self._introspect(token, tenant)
        if ident is not None:
            with self._lock:
                self._cache[key] = (ident, now + self.ttl)
        return ident

    def _introspect(self, token: str, tenant: str) -> Optional[Identity]:
        req = urllib.request.Request(self.base_url + "/v1/auth/introspect", method="GET")
        req.add_header("Authorization", "Bearer " + token)
        if tenant:
            req.add_header("X-Tenant", tenant)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not data.get("active") or not data.get("user"):
            return None
        return Identity(
            user=data["user"],
            roles=list(data.get("roles") or []),
            tenant=tenant or data.get("tenant", "default"),
            authenticated=True,
        )

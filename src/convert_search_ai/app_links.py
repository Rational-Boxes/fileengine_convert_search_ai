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

"""Resolve the **active application URL** used to build absolute file deep-links
baked into saved reports (llm_tools._linkify_file_refs) and their provenance logs.

Why this exists: a report is a *document*. It is converted to .docx, opened in
ONLYOFFICE, printed to PDF, mailed, copied elsewhere — contexts with no page origin
to resolve a relative ``/files?file=…`` against. Every reference must therefore be a
complete URL. ``CSAI_PUBLIC_APP_URL`` was the only source of that origin and is unset
in every current deployment, so every reference came out relative.

**Tenancy — why the request itself is the answer.** Tenants reach the platform
through their own doors: a subdomain (``acme.example.com``, or ``<tenant>-<interface>``
as http_auth.extract_tenant reads it), a vanity domain (``docs.acme.com``), or a
shared host that carries the tenant in ``X-Tenant``/``?tenant=``. The deep-links
belong on the same FQDN the chat was served on, and csai is reached through that same
door (nginx proxies ``/csai/`` on each tenant vhost, forwarding ``Host`` and
``X-Forwarded-Proto``). So the origin the request arrived on IS the tenant's app
origin — every tenant gets its own, with nothing to configure and no host taken from
the client, whatever mix of subdomains and domains the deployment uses.

Resolution:
  1. ``CSAI_PUBLIC_APP_URL`` — an explicit operator override for the exceptional
     deployment where the request's Host is NOT the public FQDN (csai reached
     directly, or behind a proxy that rewrites Host). A ``{tenant}`` placeholder is
     substituted with the request's tenant, so one setting still gives each tenant
     its own host.
  2. the origin the request arrived on (``X-Forwarded-Proto``/``Host``) — the normal
     path, plus the SPA's base path when it is served under a sub-path (the one part
     of the URL the server cannot know, taken from the frame's ``app_url`` and only
     when that value's origin is this same origin).
  3. ``""`` — no absolute base; links stay relative, as before.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

log = logging.getLogger("convert_search_ai.app_links")

_SAFE_SCHEMES = ("http", "https")


def normalize_app_url(raw: str) -> str:
    """Normalize a candidate app URL to ``scheme://host[:port][/prefix]``, or ``""``
    when it is unusable. Query, fragment, userinfo and any trailing slash are
    dropped; only http(s) is accepted (no ``javascript:``/``data:``/``file:``)."""
    s = (raw or "").strip()
    if not s or any(c.isspace() or ord(c) < 0x20 for c in s):
        return ""
    try:
        u = urlsplit(s)
    except ValueError:
        return ""
    if u.scheme.lower() not in _SAFE_SCHEMES or not u.netloc:
        return ""
    if "@" in u.netloc:            # strip-credentials URLs are never legitimate here
        return ""
    path = u.path.rstrip("/")
    if path and not path.startswith("/"):
        return ""
    return f"{u.scheme.lower()}://{u.netloc.lower()}{path}"


def origin_of(url: str) -> str:
    """The scheme+host+port of a normalized URL (its path prefix removed)."""
    u = urlsplit(url or "")
    return f"{u.scheme.lower()}://{u.netloc.lower()}" if u.scheme and u.netloc else ""


def request_origin(headers: dict, *, default_scheme: str = "http") -> str:
    """The origin this request arrived on — i.e. the tenant's own door — from the
    (lower-cased) request headers. Honours the reverse proxy's X-Forwarded-Proto /
    X-Forwarded-Host, since csai is proxied on the SAME FQDN the SPA is served from."""
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip()
    if not host:
        return ""
    scheme = (headers.get("x-forwarded-proto") or default_scheme).split(",")[0].strip().lower()
    if scheme not in _SAFE_SCHEMES:
        scheme = default_scheme
    return normalize_app_url(f"{scheme}://{host}")


def configured_app_url(config, tenant: str = "") -> str:
    """The operator's ``CSAI_PUBLIC_APP_URL`` override for ``tenant``, with any
    ``{tenant}`` placeholder substituted — or ``""`` when unset (the normal case)."""
    raw = (getattr(config, "public_app_url", "") or "").strip().rstrip("/")
    if not raw:
        return ""
    url = raw.replace("{tenant}", tenant or "")
    if not normalize_app_url(url):
        log.warning("ignoring unusable CSAI_PUBLIC_APP_URL %r", raw)
        return ""
    return url


def _sub_path(client_url: str, origin: str) -> str:
    """The base path the SPA is served under, from the URL the client reported —
    accepted only when the client is on THIS origin, so no host, port or scheme can
    come from the client. Anything else contributes nothing."""
    url = normalize_app_url(client_url)
    if not url or not origin or origin_of(url) != origin:
        if url and origin and origin_of(url) != origin:
            log.info("ignoring app_url %r: this chat is served on %r", url, origin)
        return ""
    return urlsplit(url).path.rstrip("/")


def resolve_app_url(config, headers: dict, *, tenant: str = "", client_url: str = "",
                    default_scheme: str = "http") -> str:
    """The app URL to bake into this turn's report links: the FQDN this chat is
    served on — which is this tenant's own door — unless an operator pinned one.
    ``headers`` are the request's, lower-cased."""
    configured = configured_app_url(config, tenant)
    if configured:
        return configured
    origin = request_origin(headers, default_scheme=default_scheme)
    if not origin:
        return ""
    return origin + _sub_path(client_url, origin)

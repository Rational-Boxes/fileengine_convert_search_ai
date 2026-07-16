"""Real client-IP resolution behind a reverse proxy (PROPOSAL §3), mirroring the
C++ bridges' ``client_ip.h`` so every tier derives the SAME client IP.

Configure ``FILEENGINE_TRUSTED_PROXIES`` (comma-separated IPs/CIDRs of the reverse
proxy). When set, X-Forwarded-For is credible only if the immediate peer is a
trusted proxy, and the client is the right-most XFF hop that is NOT itself a
trusted proxy (spoofed left-most entries are ignored). When unset (development),
the first XFF hop is trusted for convenience — do not run that way in production.
"""
from __future__ import annotations

import ipaddress
import os


def parse_trusted(raw: str) -> list:
    nets = []
    for c in (raw or "").split(","):
        c = c.strip()
        if not c:
            continue
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue
    return nets


def _is_trusted(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def resolve_client_ip(peer: str, xff_header: str, trusted=None) -> str:
    """peer = socket peer; xff_header = raw X-Forwarded-For; trusted = list of
    ip_network (defaults to FILEENGINE_TRUSTED_PROXIES)."""
    nets = parse_trusted(os.environ.get("FILEENGINE_TRUSTED_PROXIES", "")) if trusted is None else trusted
    hops = [h.strip() for h in (xff_header or "").split(",") if h.strip()]
    if not nets:  # dev: trust the first XFF hop, else the peer
        return hops[0] if hops else (peer or "")
    if not _is_trusted(peer or "", nets):
        return peer or ""       # a direct (non-proxy) peer can't spoof via XFF
    if not hops:
        return peer or ""
    for h in reversed(hops):    # right-most hop that is not a trusted proxy
        if not _is_trusted(h, nets):
            return h
    return peer or ""


def client_ip_from_request(request) -> str:
    """FastAPI convenience wrapper."""
    peer = request.client.host if request.client else ""
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for", ""))

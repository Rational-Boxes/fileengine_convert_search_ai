"""SSRF-guarded web page fetch + readable-text extraction (WEB_SEARCH_TOOL_PLAN §9).

Backs the chat ``fetch_page`` tool. Everything degrades to ``None`` on a blocked or
failing fetch — a tool never crashes the answer. Dependency-free (stdlib only); the
text extraction is a light HTML stripper, not a full readability engine.

Security posture (deliberately conservative):
  * https only;
  * the host must resolve to a PUBLIC IP — loopback/private/link-local/reserved/
    multicast targets are rejected (blocks the classic metadata-endpoint SSRF);
  * redirects are followed manually, re-validating every hop;
  * response size, redirect count, and wall-clock are all capped;
  * only text/html and text/plain bodies are read.

Caveat: DNS is re-resolved by urlopen after our check (a TOCTOU window). For a
hard guarantee one would pin the validated IP and send a Host header; that is a
future hardening — this blocks the common, non-adversarial-DNS cases.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

_log = logging.getLogger("convert_search_ai.webfetch")

_UA = "convert-search-ai/0.1 (+web_search tool)"
_ALLOWED_CONTENT = ("text/html", "text/plain", "application/xhtml+xml")


class FetchBlocked(Exception):
    """The URL was rejected by the SSRF guard (not a transient error)."""


def _ip_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def _host_is_public(host: str) -> bool:
    """True iff every address ``host`` resolves to is a public, routable IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addrs = [i[4][0] for i in infos]
    return bool(addrs) and not any(_ip_blocked(a) for a in addrs)


def validate_url(url: str) -> str:
    """Raise FetchBlocked unless ``url`` is https with a public host."""
    p = urlparse(url)
    if p.scheme != "https":
        raise FetchBlocked(f"scheme {p.scheme!r} not allowed (https only)")
    if not p.hostname:
        raise FetchBlocked("missing host")
    if not _host_is_public(p.hostname):
        raise FetchBlocked(f"host {p.hostname!r} is private/loopback/unresolvable")
    return url


class _TextExtractor(HTMLParser):
    """Collect visible text + the <title>, dropping script/style/etc."""

    _SKIP = {"script", "style", "noscript", "template", "head", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            t = data.strip()
            if t:
                self._parts.append(t)

    def text(self) -> str:
        return " ".join(self._parts)


def _opener() -> urllib.request.OpenerDirector:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        # Don't auto-follow; we re-validate each hop ourselves.
        def redirect_request(self, *a, **k):  # noqa: D401
            return None

    return urllib.request.build_opener(_NoRedirect)


def fetch_text(url: str, *, max_bytes: int = 2_000_000, timeout: float = 5.0,
               max_redirects: int = 3) -> Optional[Tuple[str, str]]:
    """Fetch ``url`` and return ``(title, text)``, or ``None`` if blocked/failed.

    Raises nothing for network/SSRF problems — logs and returns None."""
    opener = _opener()
    current = url
    try:
        for _ in range(max_redirects + 1):
            validate_url(current)
            req = urllib.request.Request(current, headers={
                "User-Agent": _UA, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
            try:
                resp = opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc:
                        return None
                    current = urljoin(current, loc)
                    continue
                _log.info("fetch_page HTTP %s for %s", e.code, current)
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in _ALLOWED_CONTENT:
                _log.info("fetch_page skipped content-type %s", ctype)
                return None
            raw = resp.read(max_bytes + 1)[:max_bytes]
            charset = resp.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, "replace")
            if ctype == "text/plain":
                return ("", body.strip())
            ex = _TextExtractor()
            ex.feed(body)
            return (ex.title.strip(), ex.text())
        _log.info("fetch_page exceeded %d redirects", max_redirects)
        return None
    except FetchBlocked as e:
        _log.warning("fetch_page blocked: %s", e)
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.info("fetch_page failed: %s", e)
        return None

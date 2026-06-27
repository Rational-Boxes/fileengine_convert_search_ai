"""SSRF guard + HTML text extraction for the fetch_page tool (offline)."""
import socket

import pytest

import convert_search_ai.webfetch as wf


def test_ip_blocked_classifies_private_and_public():
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
               "::1", "0.0.0.0", "224.0.0.1", "fc00::1"):
        assert wf._ip_blocked(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
        assert not wf._ip_blocked(ip), ip
    assert wf._ip_blocked("not-an-ip")


def _fake_resolver(mapping):
    def gai(host, *a, **k):
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return gai


def test_validate_url_enforces_https_and_public_host(monkeypatch):
    monkeypatch.setattr(wf.socket, "getaddrinfo",
                        _fake_resolver({"public.example": "93.184.216.34",
                                        "internal.host": "10.1.2.3"}))
    assert wf.validate_url("https://public.example/x") == "https://public.example/x"
    with pytest.raises(wf.FetchBlocked):
        wf.validate_url("http://public.example")          # not https
    with pytest.raises(wf.FetchBlocked):
        wf.validate_url("https://internal.host/meta")     # resolves private
    with pytest.raises(wf.FetchBlocked):
        wf.validate_url("https://does-not-resolve.example")  # unresolvable
    with pytest.raises(wf.FetchBlocked):
        wf.validate_url("https:///nohost")                # missing host


def test_text_extractor_drops_scripts_and_captures_title():
    html = ("<html><head><title>  Page Title </title><style>.x{color:red}</style></head>"
            "<body><script>evil()</script><p>Hello <b>world</b></p>"
            "<p>second line</p></body></html>")
    ex = wf._TextExtractor()
    ex.feed(html)
    assert ex.title.strip() == "Page Title"
    text = ex.text()
    assert "Hello" in text and "world" in text and "second line" in text
    assert "evil()" not in text and "color:red" not in text

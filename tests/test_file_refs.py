"""_linkify_file_refs: rewrite "(file <uid>)" report refs into named tenant links."""
from convert_search_ai.llm_tools import _linkify_file_refs

UID = "5a23e207-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


class _Info:
    def __init__(self, name):
        self.name = name


class FakeMF:
    """stat() resolves known uids, raises (like the real NotFoundError) otherwise."""
    def __init__(self, names):
        self._names = names

    def stat(self, uid, tenant=None, **kw):
        if uid not in self._names:
            raise KeyError("no such file")
        return _Info(self._names[uid])


def test_resolved_ref_becomes_named_tenant_link():
    mf = FakeMF({UID: "Q3 budget.xlsx"})
    out = _linkify_file_refs(f"<p>See (file {UID}) for details.</p>", mf, "acme")
    assert f'href="/files?file={UID}&amp;tenant=acme"' in out
    assert "📄 Q3 budget.xlsx" in out
    assert f"(file {UID})" not in out  # the raw ref is gone


def test_unresolved_ref_degrades_to_label_not_broken_link():
    out = _linkify_file_refs(f"(file {UID})", FakeMF({}), "acme")
    assert "<a" not in out
    assert "(📄 file)" in out
    assert UID not in out  # never leaks the uid to the reader


def test_name_is_html_escaped():
    out = _linkify_file_refs(f"(file {UID})", FakeMF({UID: "<script>.html"}), "t")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_no_refs_is_untouched_and_never_stats():
    class Boom(FakeMF):
        def stat(self, *a, **k):
            raise AssertionError("should not resolve when there are no refs")

    html = "<p>Plain report, no file refs.</p>"
    assert _linkify_file_refs(html, Boom({}), "t") == html


def test_wordy_or_short_parens_never_match():
    text = "open the (file cabinet) or (file 3)"
    assert _linkify_file_refs(text, FakeMF({}), "t") == text


def test_absolute_base_url_is_external_hostable():
    # A base_url makes the link absolute so it survives PDF export / external hosting.
    out = _linkify_file_refs(f"(file {UID})", FakeMF({UID: "r.html"}), "acme",
                             "https://app.example.com")
    assert f'href="https://app.example.com/files?file={UID}&amp;tenant=acme"' in out


def test_tenant_template_links_each_tenant_to_its_own_host():
    # Multi-tenant: {tenant} in the base makes each report link to its own host.
    tmpl = "https://{tenant}.example.com"
    a = _linkify_file_refs(f"(file {UID})", FakeMF({UID: "r.html"}), "acme", tmpl)
    b = _linkify_file_refs(f"(file {UID})", FakeMF({UID: "r.html"}), "globex", tmpl)
    assert 'href="https://acme.example.com/files?' in a
    assert 'href="https://globex.example.com/files?' in b
    assert "{tenant}" not in a and "{tenant}" not in b


def test_shared_cache_resolves_each_uid_once():
    calls = []

    class CountingMF(FakeMF):
        def stat(self, uid, tenant=None, **kw):
            calls.append(uid)
            return super().stat(uid, tenant=tenant)

    mf, cache = CountingMF({UID: "a.txt"}), {}
    _linkify_file_refs(f"(file {UID})", mf, "t", "", cache)
    _linkify_file_refs(f"again (file {UID})", mf, "t", "", cache)
    assert calls == [UID]  # the second call hit the shared cache

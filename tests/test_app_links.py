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

"""app_links.resolve_app_url: the application URL baked into report deep-links.

The deep-links belong on the FQDN the chat was served on, so these cover the
per-tenant doors a deployment can present — subdomain, vanity domain, shared host —
and the operator override."""
from convert_search_ai.app_links import (configured_app_url, normalize_app_url,
                                         origin_of, request_origin, resolve_app_url)


class Cfg:
    def __init__(self, public_app_url=""):
        self.public_app_url = public_app_url


def door(host, scheme="https"):
    """The headers nginx forwards for a request that arrived on ``host``."""
    return {"host": host, "x-forwarded-proto": scheme}


ACME = door("acme.example.com")


# ------------------------------------------------------------------ normalizing
def test_normalize_keeps_scheme_host_and_path_prefix():
    assert normalize_app_url("https://files.example.com/app/") == "https://files.example.com/app"
    assert normalize_app_url("HTTPS://Files.Example.COM:8443") == "https://files.example.com:8443"
    assert normalize_app_url("https://h/app?x=1#f") == "https://h/app"


def test_normalize_rejects_unusable_values():
    for bad in ("", "   ", "/files", "javascript:alert(1)", "data:text/html,x",
                "file:///etc/passwd", "https://", "https://u:p@evil.example.com",
                "https://h\n.evil"):
        assert normalize_app_url(bad) == "", bad


def test_request_origin_honours_the_reverse_proxy():
    assert request_origin(ACME) == "https://acme.example.com"
    assert request_origin({"host": "csai:8092"}) == "http://csai:8092"
    assert request_origin({"x-forwarded-host": "a.example.com, b", "x-forwarded-proto": "https"}) \
        == "https://a.example.com"
    assert request_origin({}) == ""


def test_origin_of_strips_the_path_prefix():
    assert origin_of("https://h.example.com/app") == "https://h.example.com"


# ------------------------------------------------------ each tenant's own door
def test_each_tenant_links_to_the_fqdn_its_chat_was_served_on():
    # Subdomain per tenant and vanity domain per tenant are the same case: the door
    # the request arrived on is the tenant's app origin. Nothing to configure.
    assert resolve_app_url(Cfg(), ACME, tenant="acme") == "https://acme.example.com"
    assert resolve_app_url(Cfg(), door("globex-drive.example.com"), tenant="globex") \
        == "https://globex-drive.example.com"
    assert resolve_app_url(Cfg(), door("docs.acme.com"), tenant="acme") == "https://docs.acme.com"


def test_a_shared_host_serves_every_tenant_from_the_same_door():
    # One FQDN, tenants told apart by X-Tenant/?tenant= — the link host is the same
    # for both, and the tenant travels in the deep-link's ?tenant= query.
    shared = door("files.example.com")
    assert resolve_app_url(Cfg(), shared, tenant="acme") == "https://files.example.com"
    assert resolve_app_url(Cfg(), shared, tenant="globex") == "https://files.example.com"


def test_port_and_scheme_of_the_door_are_preserved():
    assert resolve_app_url(Cfg(), {"host": "acme.example.com:8443", "x-forwarded-proto": "https"},
                           tenant="acme") == "https://acme.example.com:8443"
    assert resolve_app_url(Cfg(), {"host": "localhost:5173"}, tenant="default") \
        == "http://localhost:5173"


def test_ws_scheme_is_the_default_when_the_proxy_sends_no_forwarded_proto():
    assert resolve_app_url(Cfg(), {"host": "acme.example.com"}, tenant="acme",
                           default_scheme="https") == "https://acme.example.com"


# ------------------------------------------- the SPA's sub-path (client-supplied)
def test_spa_sub_path_is_taken_from_the_client_on_the_same_origin():
    # The base path the SPA is deployed under is the one thing the server cannot see.
    got = resolve_app_url(Cfg(), ACME, tenant="acme",
                          client_url="https://acme.example.com/app")
    assert got == "https://acme.example.com/app"


def test_client_can_never_change_the_host_only_the_path():
    # A stranger's host, and another tenant's host, are both ignored: the FQDN always
    # comes from the request. Nothing a client says can put a look-alike link inside a
    # document stored in this tenant's storage.
    assert resolve_app_url(Cfg(), ACME, tenant="acme",
                           client_url="https://evil.example.com/app") == "https://acme.example.com"
    assert resolve_app_url(Cfg(), ACME, tenant="acme",
                           client_url="https://globex.example.com") == "https://acme.example.com"


# --------------------------------------------------------- the operator override
def test_configured_url_pins_the_app_origin():
    cfg = Cfg("https://app.example.com/")
    assert resolve_app_url(cfg, door("csai.internal"), tenant="acme") == "https://app.example.com"


def test_configured_template_gives_each_tenant_its_own_host():
    cfg = Cfg("https://{tenant}.example.com")
    assert resolve_app_url(cfg, door("csai.internal"), tenant="acme") == "https://acme.example.com"
    assert resolve_app_url(cfg, door("csai.internal"), tenant="globex") \
        == "https://globex.example.com"


def test_configured_app_url_substitutes_the_tenant_and_rejects_junk():
    assert configured_app_url(Cfg("https://{tenant}.example.com/"), "acme") \
        == "https://acme.example.com"
    assert configured_app_url(Cfg("not-a-url"), "acme") == ""
    assert configured_app_url(Cfg(), "acme") == ""


# ---------------------------------------------------------------- last resort
def test_no_host_header_at_all_degrades_to_relative_links():
    assert resolve_app_url(Cfg(), {}, tenant="acme", client_url="https://evil.example.com") == ""

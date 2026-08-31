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

"""The deployment's feature configuration, as the SPA reads it."""

import convert_search_ai.core_client as core_client
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity

CAPS = "/v1/capabilities"


def _app(monkeypatch, **cfgover):
    cfg = Config()
    cfg.onlyoffice_enabled = True
    cfg.onlyoffice_docserver_url = "http://localhost:8080"
    cfg.onlyoffice_signing_secret = "sign-secret"
    for k, v in cfgover.items():
        setattr(cfg, k, v)

    def explode(identity, config):
        # A deployment-level question must not need the core.
        raise AssertionError("capabilities must not touch the core client")

    monkeypatch.setattr(core_client, "client_for", explode)
    app = build_app(cfg)
    return app, TestClient(app)


def _auth(app, user="alice", roles=("users",)):
    tok = app.state.token_store.issue(
        Identity(user=user, roles=list(roles), tenant="default", authenticated=True))
    return {"Authorization": f"Bearer {tok}"}


def _caps(monkeypatch, **cfgover):
    app, c = _app(monkeypatch, **cfgover)
    r = c.get(CAPS, headers=_auth(app))
    assert r.status_code == 200, r.text
    return r.json()


def test_requires_authentication(monkeypatch):
    _, c = _app(monkeypatch)
    assert c.get(CAPS).status_code == 401


def test_reports_every_section(monkeypatch):
    body = _caps(monkeypatch)
    # Namespaced so a client reads what it understands and ignores the rest.
    assert set(body) >= {"editing", "chat", "web_search", "search"}


# --------------------------------- editing ----------------------------------

def test_editing_available_when_fully_configured(monkeypatch):
    ed = _caps(monkeypatch)["editing"]
    assert ed["available"] is True and ed["reason"] == ""
    assert "docx" in ed["extensions"] and "xlsx" in ed["extensions"]
    assert "png" not in ed["extensions"]


def test_editing_off_when_the_feature_is_switched_off(monkeypatch):
    ed = _caps(monkeypatch, onlyoffice_enabled=False)["editing"]
    assert ed["available"] is False and "CSAI_ONLYOFFICE_ENABLED" in ed["reason"]


def test_editing_off_without_a_docserver(monkeypatch):
    ed = _caps(monkeypatch, onlyoffice_docserver_url="")["editing"]
    assert ed["available"] is False and "DOCSERVER_URL" in ed["reason"]


def test_editing_off_without_a_signing_secret(monkeypatch):
    # The condition that used to be reachable only by clicking Edit: the other
    # two are checked up front, the secret only when a config is being signed.
    ed = _caps(monkeypatch, onlyoffice_signing_secret="")["editing"]
    assert ed["available"] is False and "signing secret" in ed["reason"]


# ----------------------------------- chat -----------------------------------

def test_chat_needs_a_key_for_a_hosted_provider(monkeypatch):
    assert _caps(monkeypatch, chat_provider="anthropic", chat_api_key="")["chat"]["available"] is False
    assert _caps(monkeypatch, chat_provider="anthropic", chat_api_key="k")["chat"]["available"] is True


def test_chat_needs_no_key_for_a_local_provider(monkeypatch):
    # Ollama authenticates by being reachable, not by a key — requiring one
    # would report a working deployment as having no chat.
    assert _caps(monkeypatch, chat_provider="ollama", chat_api_key="")["chat"]["available"] is True


# ---------------------------------- search ----------------------------------

def test_search_off_without_an_embedding_model(monkeypatch):
    # Nothing was ever indexed, so the search box would answer every query with
    # nothing and look like an empty library rather than a missing feature.
    assert _caps(monkeypatch, embedding_provider="")["search"]["available"] is False


def test_search_needs_a_key_for_a_hosted_embedder(monkeypatch):
    assert _caps(monkeypatch, embedding_provider="openai-compatible",
                 embedding_api_key="")["search"]["available"] is False
    assert _caps(monkeypatch, embedding_provider="openai-compatible",
                 embedding_api_key="k")["search"]["available"] is True


# -------------------------------- web search --------------------------------

def test_web_search_reports_both_availability_and_its_default(monkeypatch):
    ws = _caps(monkeypatch, web_search_enabled=True, web_search_default=False)["web_search"]
    assert ws["available"] is True and ws["default_on"] is False


# --------------------------------- secrets ----------------------------------

def test_never_reports_a_secret(monkeypatch):
    """The one rule a configuration endpoint has to keep.

    Reported as configuration is not the same as safe to report: an API key or a
    signing secret is neither, and this is the test that says so out loud rather
    than trusting each future section to remember.
    """
    body = _caps(monkeypatch,
                 chat_api_key="CHAT-SECRET",
                 embedding_api_key="EMBED-SECRET",
                 onlyoffice_signing_secret="SIGN-SECRET",
                 onlyoffice_jwt_secret="JWT-SECRET")
    import json
    blob = json.dumps(body)
    for secret in ("CHAT-SECRET", "EMBED-SECRET", "SIGN-SECRET", "JWT-SECRET"):
        assert secret not in blob, f"{secret} leaked into the capabilities document"

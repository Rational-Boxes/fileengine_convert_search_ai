"""WebSocket /chat endpoint tests (TestClient + injected fake chat service)."""
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config
from convert_search_ai.ldap_auth import Identity


class FakeChat:
    def answer(self, identity, *, message, system_prompt="", history=None, k=8):
        yield {"type": "token", "text": f"Answer to: {message}"}
        yield {"type": "citations", "citations": [{"file_uid": "f1", "marker": 1}]}


def _client():
    app = build_app(Config(), chat=FakeChat())
    return app, TestClient(app)


def _token(app, user="alice"):
    return app.state.token_store.issue(
        Identity(user=user, roles=["administrators"], tenant="default", authenticated=True))


def test_chat_rejects_unauthenticated():
    _, c = _client()
    with c.websocket_connect("/chat") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "authentication" in msg["error"]


def test_chat_streams_tokens_then_citations_then_done():
    app, c = _client()
    tok = _token(app)
    with c.websocket_connect(f"/chat?token={tok}") as ws:
        ws.send_json({"message": "what is x?", "system_prompt": "Be helpful"})
        events = []
        while True:
            e = ws.receive_json()
            events.append(e)
            if e["type"] == "done":
                break
    types = [e["type"] for e in events]
    assert "token" in types and "citations" in types and types[-1] == "done"
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "what is x?" in text
    cites = [e for e in events if e["type"] == "citations"][0]["citations"]
    assert cites[0]["file_uid"] == "f1"


def test_chat_requires_message():
    app, c = _client()
    with c.websocket_connect(f"/chat?token={_token(app)}") as ws:
        ws.send_json({"system_prompt": "hi"})
        assert ws.receive_json() == {"type": "error", "error": "message is required"}

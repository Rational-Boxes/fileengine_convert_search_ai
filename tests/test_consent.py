"""ConsentBroker: the async-socket <-> sync-tool-thread consent bridge."""
import threading
import time
from types import SimpleNamespace

from convert_search_ai.consent import ConsentBroker


def _req(tool_full="mcp__crm__create_ticket"):
    return SimpleNamespace(integration="CRM", slug="crm", tool="create_ticket",
                           tool_full=tool_full, args_summary="subject=Hi")


def test_approve_returns_true_and_emits_request():
    emitted = []
    b = ConsentBroker(emitted.append, timeout_s=5)
    # Resolve from another thread once the request event has been emitted.
    def approver():
        while not emitted:
            time.sleep(0.001)
        b.resolve(emitted[0]["id"], decision=True)
    threading.Thread(target=approver).start()
    assert b.request(_req()) is True
    assert emitted[0]["type"] == "tool_consent_request"
    assert emitted[0]["integration"] == "CRM" and emitted[0]["tool"] == "create_ticket"


def test_deny_returns_false():
    emitted = []
    b = ConsentBroker(emitted.append, timeout_s=5)
    threading.Thread(target=lambda: (_wait(emitted), b.resolve(emitted[0]["id"], False))).start()
    assert b.request(_req()) is False


def test_timeout_defaults_to_deny():
    b = ConsentBroker(lambda e: None, timeout_s=0.05)
    t0 = time.time()
    assert b.request(_req()) is False
    assert time.time() - t0 < 2  # returned promptly on timeout, didn't hang


def test_remember_skips_second_prompt():
    emitted = []
    remembered = set()
    b = ConsentBroker(emitted.append, timeout_s=5, remembered=remembered)
    threading.Thread(target=lambda: (_wait(emitted),
                                     b.resolve(emitted[0]["id"], True, remember=True))).start()
    assert b.request(_req()) is True
    assert "mcp__crm__create_ticket" in remembered
    # Second call is auto-approved with NO new prompt emitted.
    before = len(emitted)
    assert b.request(_req()) is True
    assert len(emitted) == before


def test_shutdown_unblocks_pending_as_denied():
    emitted = []
    b = ConsentBroker(emitted.append, timeout_s=30)
    result = {}
    th = threading.Thread(target=lambda: result.__setitem__("r", b.request(_req())))
    th.start()
    _wait(emitted)
    b.shutdown()
    th.join(timeout=2)
    assert result.get("r") is False
    # A further request after shutdown denies immediately.
    assert b.request(_req("mcp__crm__other")) is False


def _wait(emitted, timeout=2):
    t0 = time.time()
    while not emitted and time.time() - t0 < timeout:
        time.sleep(0.001)

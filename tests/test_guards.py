"""Unit tests for the request guardrails."""
import pytest

from convert_search_ai import guards


class _C:
    def __init__(self, text):
        self.text = text


def test_check_query_trims_and_rejects_empty():
    assert guards.check_query("  hello  ", 100) == "hello"
    with pytest.raises(guards.GuardError):
        guards.check_query("   ", 100)


def test_check_query_length_cap():
    assert guards.check_query("x" * 100, 100) == "x" * 100
    with pytest.raises(guards.GuardError):
        guards.check_query("x" * 101, 100)
    assert guards.check_query("x" * 9999, 0) == "x" * 9999  # 0 = unlimited


def test_cap_limit_and_k():
    assert guards.cap_limit(500, 100) == 100
    assert guards.cap_limit(5, 100) == 5
    assert guards.cap_limit(0, 100) == 1     # always at least 1
    assert guards.cap_limit(7, 0) == 7       # 0 = unlimited
    assert guards.cap_k(50, 12) == 12


def test_trim_context_keeps_first_and_flags_trim():
    chunks = [_C("a" * 100), _C("b" * 100), _C("c" * 100)]
    kept, trimmed = guards.trim_context(chunks, 150)
    assert len(kept) == 1 and trimmed                    # second would exceed budget
    assert guards.trim_context(chunks, 0) == (chunks, False)   # 0 = unlimited
    kept3, t3 = guards.trim_context([_C("x" * 500)], 100)
    assert len(kept3) == 1 and t3                        # oversized first chunk kept, flagged


def test_cap_text_bytes():
    assert guards.cap_text_bytes("hello", 100) == ("hello", False)
    capped, truncated = guards.cap_text_bytes("a" * 200, 50)
    assert truncated and len(capped.encode("utf-8")) <= 50
    assert guards.cap_text_bytes("a" * 200, 0) == ("a" * 200, False)  # unlimited

"""Per-key limits and the usage status (HEADLESS_RESEARCH_API.md §3.8).

The meter runs over the in-memory backend with a controlled clock, so windows,
states, refusals and the status object are asserted exactly; the Redis backend's
one rule — any error is UsageUnavailable, never "let it through" — is asserted
with a client that fails."""
import pytest

from convert_search_ai.headless import usage as u
from convert_search_ai.ldap_auth import Identity

KEY = Identity(user="alice", tenant="t", authenticated=True,
               client_key_id="fesk_a", client_label="Reviera — laptop")
OTHER_KEY = Identity(user="alice", tenant="t", authenticated=True, client_key_id="fesk_b")
NO_KEY = Identity(user="alice", tenant="t", authenticated=True)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def meter(**kw):
    clock = Clock()
    limits = u.Limits(requests_per_minute=kw.get("rpm", 5), tokens_per_day=kw.get("tpd", 1000),
                      concurrent_runs=kw.get("conc", 2), files_per_run=kw.get("fpr", 50),
                      warn_ratio=0.8)
    return u.UsageMeter(limits, u.MemoryBackend(clock), clock=clock), clock


def test_fresh_key_is_ok_with_full_status_object():
    m, _ = meter()
    st = m.status(KEY)
    assert st["state"] == "ok" and st["subject"] == "key"
    assert set(st["limits"]) == {"requests", "tokens", "concurrent", "files_per_run"}
    assert st["limits"]["tokens"]["limit"] == 1000 and st["message"] == ""
    assert st["limits"]["requests"]["resets_at"].endswith("+00:00")


def test_requests_per_minute_warn_then_refuse_then_reset():
    m, clock = meter(rpm=5)
    for _ in range(4):
        st = m.admit(KEY, spends_tokens=False)
    assert st["limits"]["requests"]["state"] == "warning"      # 4 of 5 >= 80%
    m.admit(KEY, spends_tokens=False)                           # 5th still allowed
    with pytest.raises(u.RateLimited) as e:
        m.admit(KEY, spends_tokens=False)
    assert e.value.limit == "requests" and e.value.status["state"] == "exceeded"
    clock.t += 61                                               # next minute window
    assert m.admit(KEY, spends_tokens=False)["limits"]["requests"]["used"] == 1


def test_limits_are_per_key_and_a_keyless_session_is_counted_per_user():
    m, _ = meter(rpm=1)
    m.admit(KEY, spends_tokens=False)
    m.admit(OTHER_KEY, spends_tokens=False)     # another key is its own bucket
    m.admit(NO_KEY, spends_tokens=False)        # so is the user's own session
    with pytest.raises(u.RateLimited):
        m.admit(KEY, spends_tokens=False)
    assert m.status(NO_KEY)["subject"] == "user"


def test_token_budget_refuses_model_routes_but_not_search():
    m, clock = meter(tpd=1000)
    m.add_tokens(KEY, 850)
    st = m.status(KEY)
    assert st["limits"]["tokens"]["state"] == "warning"
    assert "85% of today's token budget" in st["message"]
    m.add_tokens(KEY, 200)
    assert m.tokens_exhausted(KEY)
    with pytest.raises(u.RateLimited) as e:
        m.admit(KEY, spends_tokens=True)
    assert e.value.limit == "tokens"
    m.admit(KEY, spends_tokens=False)           # a search spends no tokens: allowed
    clock.t += 86400                            # next UTC day
    assert not m.tokens_exhausted(KEY)


def test_concurrent_runs_slot_is_held_and_released():
    m, _ = meter(conc=1)
    with m.run(KEY):
        assert m.status(KEY)["limits"]["concurrent"]["used"] == 1
        with pytest.raises(u.RateLimited) as e:
            with m.run(KEY):
                pass
        assert e.value.limit == "concurrent"
    assert m.status(KEY)["limits"]["concurrent"]["used"] == 0


def test_cache_hits_count_tokens_saved_not_spent():
    m, _ = meter()
    m.add_cache_hit(KEY, 420)
    st = m.status(KEY)
    assert st["cache"] == {"hits": 1, "tokens_saved_estimate": 420}
    assert st["limits"]["tokens"]["used"] == 0


def test_headers_carry_the_state_and_the_request_window():
    m, _ = meter(rpm=5)
    st = m.admit(KEY, spends_tokens=False)
    h = u.headers(st)
    assert h["X-Usage-State"] == "ok"
    assert h["RateLimit-Policy"] == '"requests";q=5;w=60'
    assert h["RateLimit"].startswith('"requests";r=4;t=')


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise ConnectionError("redis down")
        return fail


def test_redis_errors_refuse_rather_than_count_nothing():
    m = u.UsageMeter(u.Limits(5, 1000, 2, 50), u.RedisBackend(BrokenRedis()))
    with pytest.raises(u.UsageUnavailable):
        m.admit(KEY, spends_tokens=True)
    with pytest.raises(u.UsageUnavailable):
        m.status(KEY)

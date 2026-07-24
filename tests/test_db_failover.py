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

"""db.connect master/replica routing + read-only fallback (REPLICATION_FAILOVER.md).

psycopg is monkeypatched so no real database is needed; the breaker uses an
injected clock for deterministic recovery."""
import sys
import types

import pytest

from convert_search_ai import db
from convert_search_ai.failover import CircuitBreaker, DegradedReadOnly


class _OpError(Exception):
    pass


class _Conn:
    def __init__(self, which):
        self.which = which


def _install_fake_psycopg(monkeypatch, master_up: bool):
    state = {"master_up": master_up, "calls": []}

    def connect(dsn):
        state["calls"].append(dsn)
        if "host=masterhost" in dsn:
            if not state["master_up"]:
                raise _OpError("primary down")
            return _Conn("master")
        return _Conn("replica")

    fake = types.SimpleNamespace(connect=connect, OperationalError=_OpError)
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return state


def _cfg(replica=True):
    return types.SimpleNamespace(
        pg_dsn="host=masterhost port=5432 dbname=d user=u password=p",
        pg_replica_enabled=replica,
        pg_replica_dsn="host=replicahost port=5432 dbname=d user=u password=p",
        failover_cooldown_s=30,
    )


@pytest.fixture
def clock():
    t = {"v": 0.0}
    # fresh breaker with an injected clock for each test
    db._breaker = CircuitBreaker(cooldown_s=30, clock=lambda: t["v"])
    yield t
    db._breaker = None


def test_no_replica_always_uses_master(monkeypatch):
    db._breaker = None
    _install_fake_psycopg(monkeypatch, master_up=True)
    cfg = _cfg(replica=False)
    assert db.connect(cfg).which == "master"
    assert db.connect(cfg, readonly=True).which == "master"


def test_master_up_serves_reads_and_writes_from_master(monkeypatch, clock):
    _install_fake_psycopg(monkeypatch, master_up=True)
    cfg = _cfg()
    assert db.connect(cfg, readonly=False).which == "master"   # write
    assert db.connect(cfg, readonly=True).which == "master"    # read


def test_write_while_master_down_raises_degraded(monkeypatch, clock):
    _install_fake_psycopg(monkeypatch, master_up=False)
    cfg = _cfg()
    with pytest.raises(DegradedReadOnly):
        db.connect(cfg, readonly=False)
    assert db._breaker.is_degraded()


def test_read_falls_back_to_replica_when_master_down(monkeypatch, clock):
    _install_fake_psycopg(monkeypatch, master_up=False)
    cfg = _cfg()
    assert db.connect(cfg, readonly=True).which == "replica"
    assert db._breaker.is_degraded()


def test_while_degraded_write_fails_fast_without_touching_master(monkeypatch, clock):
    state = _install_fake_psycopg(monkeypatch, master_up=False)
    cfg = _cfg()
    db.connect(cfg, readonly=True)           # trips the breaker (fell back to replica)
    state["calls"].clear()
    with pytest.raises(DegradedReadOnly):
        db.connect(cfg, readonly=False)      # within cooldown -> immediate, no master dial
    assert not any("masterhost" in c for c in state["calls"])


def test_recovery_after_cooldown_resumes_master(monkeypatch, clock):
    state = _install_fake_psycopg(monkeypatch, master_up=False)
    cfg = _cfg()
    db.connect(cfg, readonly=True)           # master down -> replica, breaker tripped
    assert db._breaker.is_degraded()

    state["master_up"] = True                # master recovers
    clock["v"] = 30.0                        # cooldown elapsed -> re-probe
    assert db.connect(cfg, readonly=True).which == "master"
    assert not db._breaker.is_degraded()     # breaker reset
    assert db.connect(cfg, readonly=False).which == "master"  # writes resume

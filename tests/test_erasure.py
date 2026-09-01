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

"""Honouring a platform erasure (PROPOSAL_accountability_record.md §5.4).

csai holds the sharpest derived data on the platform: extracted text verbatim in
the full-text index, and embeddings that are lossy but real derivatives. An
erasure that left those in place would not meet any purge obligation worth
signing, so these assert on what is actually destroyed and on what is reported
back — the acknowledgement is the evidence an auditor is shown.
"""
import pytest

from convert_search_ai.ingest import Ingestor, ERASURE_PARTICIPANT
from fakes import FakeStore


class FakeCore:
    def __init__(self, pending=None, fail_ack=False):
        self._pending = pending or {}
        self.acks = []
        self.fail_ack = fail_ack

    def list_pending_erasures(self, participant, limit=0, tenant=None, all_tenants=True):
        assert participant == ERASURE_PARTICIPANT
        # The real sweep asks for EVERY tenant in one call and reads the tenant
        # off each row. A fake that only answered per-tenant would keep passing
        # a sweep that had gone back to guessing.
        assert all_tenants, "the sweep must ask for all tenants, not a guessed list"
        rows = []
        for t, items in self._pending.items():
            for it in items:
                rows.append({**it, "tenant": t})
        return rows

    def acknowledge_erasure(self, erasure_id, participant, complied=True, detail="",
                            tenant=None):
        if self.fail_ack:
            raise RuntimeError("core unreachable")
        self.acks.append({"erasure_id": erasure_id, "participant": participant,
                          "complied": complied, "detail": detail, "tenant": tenant})
        return "complete"


def _ingestor(store=None, core=None, pending=None):
    ing = Ingestor.__new__(Ingestor)          # no gRPC, no Redis, no Postgres
    ing.config = object()
    ing.store = store or FakeStore()
    ing._provisioned = {"acme"}
    ing._core = core or FakeCore(pending=pending)
    ing._ensure_tenant = lambda tenant: None
    return ing


def test_erasure_is_not_treated_as_a_soft_delete():
    """file.erased and file.deleted must not share a branch.

    A soft delete is recoverable — the core has undelete — so keeping the index
    entry is reasonable. An erasure is not, and reusing that path would leave the
    extracted text and the vectors exactly where they were.
    """
    store, core = FakeStore(), FakeCore()
    ing = _ingestor(store, core)

    ing.handle({"type": "file.deleted", "file_uid": "u1", "tenant": "acme"})
    assert ("acme", "u1") in store.deleted
    assert not store.is_erased("acme", "u1"), "a soft delete must not tombstone"

    ing.handle({"type": "file.erased", "file_uid": "u2", "tenant": "acme",
                "erasure_id": "e2"})
    assert store.is_erased("acme", "u2")


def test_the_acknowledgement_says_what_was_destroyed():
    # "acknowledged" with no statement of what was destroyed is not evidence of
    # anything, and the completion record is the whole contractual value here.
    store = FakeStore()
    store.erase = lambda tenant, uid, erasure_id="": {"chunks": 7, "extracted_text": True}
    store.is_erased = lambda tenant, uid: True
    core = FakeCore()
    ing = _ingestor(store, core)

    ing.handle({"type": "file.erased", "file_uid": "u1", "tenant": "acme",
                "erasure_id": "e1"})

    assert len(core.acks) == 1
    ack = core.acks[0]
    assert ack["erasure_id"] == "e1"
    assert ack["participant"] == ERASURE_PARTICIPANT
    assert ack["complied"] is True
    assert "7 chunk(s)" in ack["detail"]
    assert "embeddings" in ack["detail"]


def test_a_failure_is_reported_as_a_failure_not_swallowed():
    """A service that cannot comply is an unmet obligation, and must look like one.

    Acknowledging anyway would close a contractual obligation that was never
    met — in the very record an auditor is shown.
    """
    store = FakeStore()
    def boom(tenant, uid, erasure_id=""):
        raise RuntimeError("disk on fire")
    store.erase = boom
    core = FakeCore()
    ing = _ingestor(store, core)

    with pytest.raises(RuntimeError):
        ing.handle({"type": "file.erased", "file_uid": "u1", "tenant": "acme",
                    "erasure_id": "e1"})

    assert len(core.acks) == 1
    assert core.acks[0]["complied"] is False
    assert "disk on fire" in core.acks[0]["detail"]


def test_a_lost_acknowledgement_does_not_undo_the_destruction():
    # Losing the ack delays completion, which is the safe direction. The unsafe
    # one would be recording compliance we cannot demonstrate — or rolling back
    # a destruction that already happened.
    store = FakeStore()
    ing = _ingestor(store, FakeCore(fail_ack=True))

    ing.handle({"type": "file.erased", "file_uid": "u1", "tenant": "acme",
                "erasure_id": "e1"})

    assert store.is_erased("acme", "u1"), "the data stays destroyed"


def test_the_sweep_picks_up_what_the_event_did_not():
    """The guarantee path. The bus is fail-open and drop-oldest by design, so a
    dropped erasure event would otherwise leave us holding data the platform has
    certified destroyed — silently."""
    store = FakeStore()
    core = FakeCore(pending={"acme": [{"erasure_id": "e9", "uid": "u9",
                                       "tenant": "acme", "initiated_at": 1}]})
    ing = _ingestor(store, core)

    assert ing.sweep_erasures() == 1
    assert store.is_erased("acme", "u9")
    assert core.acks[0]["erasure_id"] == "e9"


def test_the_sweep_reaches_a_tenant_this_worker_has_never_served():
    """The failure this replaced: sweeping only tenants we had seen traffic for.

    A worker that has not served a tenant since starting still owes it any
    erasure recorded there. Guessing the tenant set from local activity was wrong
    in the quiet direction — the erasure sat unacknowledged for ever, and nothing
    said so. Verified in production: an erasure in `filenginetest` was picked up
    by csai and ignored by the two services that guessed `default`.
    """
    core = FakeCore(pending={"never-seen": [{"erasure_id": "e1", "uid": "u1",
                                             "initiated_at": 1}]})
    ing = _ingestor(FakeStore(), core)
    assert ing.sweep_erasures() == 1
    assert core.acks[0]["tenant"] == "never-seen", "acknowledged in the row's own tenant"

"""Unit tests for the ingest worker's event→action mapping (fakes)."""
from conftest import live_db
from convert_search_ai.config import Config
from convert_search_ai.ingest import Ingestor
from fakes import FakeStore


class RecordingPipeline:
    def __init__(self):
        self.converted = []

    def convert(self, uid, tenant):
        self.converted.append((uid, tenant))
        from convert_search_ai.pipeline import ConvertOutcome
        return ConvertOutcome(uid, "converted", [])


class FakeSource:
    def __init__(self, batch):
        self.batch = list(batch)
        self.acked = []
        self.pending = []   # delivered-but-unacked (the consumer's PEL)

    def read(self, count=32, block_ms=5000):
        b, self.batch = self.batch, []
        self.pending.extend(b)   # delivered -> pending until acked
        return b

    def read_pending(self, count=32):
        return list(self.pending)

    def ack(self, ids):
        self.acked.extend(ids)
        self.pending = [(mid, ev) for (mid, ev) in self.pending if mid not in ids]

    def ensure_group(self):
        pass


def _ingestor(batch=None):
    pipe = RecordingPipeline()
    store = FakeStore()
    src = FakeSource(batch or [])
    return Ingestor(Config(), pipe, store, src), pipe, store, src


@live_db  # convert path connects to Postgres (CSAI_PG_*)
def test_file_created_and_updated_trigger_convert():
    ing, pipe, _, _ = _ingestor()
    ing.handle({"type": "file.created", "file_uid": "a", "tenant": "default"})
    ing.handle({"type": "file.updated", "file_uid": "b", "tenant": "t2"})
    assert pipe.converted == [("a", "default"), ("b", "t2")]


def test_rendition_events_are_ignored():
    ing, pipe, _, _ = _ingestor()
    ing.handle({"type": "file.created", "file_uid": "r", "tenant": "default", "is_rendition": True})
    assert pipe.converted == []


@live_db  # delete path drops document rows in Postgres (CSAI_PG_*)
def test_delete_drops_document_rows():
    ing, pipe, store, _ = _ingestor()
    ing.handle({"type": "file.deleted", "file_uid": "x", "tenant": "default"})
    assert store.deleted == [("default", "x")]
    assert pipe.converted == []


def test_dir_and_governance_events_ignored_by_conversion():
    ing, pipe, store, _ = _ingestor()
    for t in ("dir.created", "dir.deleted", "acl.changed", "role.assigned"):
        ing.handle({"type": t, "file_uid": "z", "tenant": "default"})
    assert pipe.converted == [] and store.deleted == []


def test_run_once_acks_every_message():
    batch = [("1-0", {"type": "file.created", "file_uid": "a", "tenant": "default"}),
             ("2-0", {"type": "file.deleted", "file_uid": "b", "tenant": "default"})]
    ing, pipe, store, src = _ingestor(batch)
    n = ing.run_once()
    assert n == 2
    assert src.acked == ["1-0", "2-0"]


def test_bad_event_is_acked_and_does_not_stall():
    # An event that makes handle raise must still be acked (idempotent + reconcile backstop).
    class Boom(RecordingPipeline):
        def convert(self, uid, tenant):
            raise RuntimeError("boom")

    src = FakeSource([("9-0", {"type": "file.created", "file_uid": "a", "tenant": "default"})])
    ing = Ingestor(Config(), Boom(), FakeStore(), src)
    ing._provisioned.add("default")
    assert ing.run_once() == 1
    assert src.acked == ["9-0"]


def test_read_only_failover_pauses_polls_and_recovers():
    """When the core is read-only (WriteUnavailableError), the worker leaves the
    event un-acked, sleeps, and retries from the pending list until it recovers —
    never dropping it."""
    from convert_search_ai._client import WriteUnavailableError
    from convert_search_ai.pipeline import ConvertOutcome

    class Flaky:
        def __init__(self):
            self.calls = 0

        def convert(self, uid, tenant):
            self.calls += 1
            if self.calls <= 2:          # core read-only for the first two tries
                raise WriteUnavailableError("read-only mode", operation="put", uid=uid)
            return ConvertOutcome(uid, "converted", [])

    src = FakeSource([("5-0", {"type": "file.created", "file_uid": "a", "tenant": "default"})])
    ing = Ingestor(Config(), Flaky(), FakeStore(), src)
    ing._provisioned.add("default")      # skip DB provisioning in this unit test
    sleeps: list = []
    ing._sleep = lambda s: sleeps.append(s)

    # 1st pass: new event, write rejected -> degraded, un-acked, slept once.
    assert ing.run_once() == 0
    assert ing.degraded is True
    assert src.acked == []
    assert len(sleeps) == 1

    # 2nd pass: still read-only -> retries the *pending* event, slept again, still un-acked.
    assert ing.run_once() == 0
    assert ing.degraded is True
    assert src.acked == []
    assert len(sleeps) == 2

    # 3rd pass: primary recovered -> pending event converts and is finally acked.
    assert ing.run_once() == 1
    assert src.acked == ["5-0"]

    # 4th pass: pending drained -> degraded cleared, back to normal reads.
    assert ing.run_once() == 0
    assert ing.degraded is False
    # The event was processed exactly once successfully (idempotent retries before).
    assert ing.pipeline.calls == 3

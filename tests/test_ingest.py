"""Unit tests for the ingest worker's event→action mapping (fakes)."""
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
        self.batch = batch
        self.acked = []

    def read(self, count=32, block_ms=5000):
        b, self.batch = self.batch, []
        return b

    def ack(self, ids):
        self.acked.extend(ids)

    def ensure_group(self):
        pass


def _ingestor(batch=None):
    pipe = RecordingPipeline()
    store = FakeStore()
    src = FakeSource(batch or [])
    return Ingestor(Config(), pipe, store, src), pipe, store, src


def test_file_created_and_updated_trigger_convert():
    ing, pipe, _, _ = _ingestor()
    ing.handle({"type": "file.created", "file_uid": "a", "tenant": "default"})
    ing.handle({"type": "file.updated", "file_uid": "b", "tenant": "t2"})
    assert pipe.converted == [("a", "default"), ("b", "t2")]


def test_rendition_events_are_ignored():
    ing, pipe, _, _ = _ingestor()
    ing.handle({"type": "file.created", "file_uid": "r", "tenant": "default", "is_rendition": True})
    assert pipe.converted == []


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
    assert ing.run_once() == 1
    assert src.acked == ["9-0"]

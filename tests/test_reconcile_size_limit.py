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

"""The size limit on unattended conversion.

Reported from production: preview generation stopped, the reconcile never
finished, and the worker kept restarting. A large document does not fail to
convert — the content is read whole into memory and the converter holds several
derived copies of it, so the PROCESS dies and no ``except`` in the sweep ever
runs.

Under restart-always that is a permanent loop rather than one bad file: the
killed run leaves the row at 'converting', 'converting' is the first status the
sweep retries, so every restart dies in the same place and nothing behind that
file is ever converted.

So the unattended paths refuse a file above a limit, BEFORE reading it. The
on-demand path — a person asking for one specific file — is deliberately not
limited.
"""
from convert_search_ai.pipeline import ConversionPipeline
from convert_search_ai.plugins.registry import PluginRegistry
from convert_search_ai.reconcile import reconcile_tenant, sweep_tenant
from convert_search_ai.store import DocRow
from fakes import FakeEntry, FakeMF, FakeStore
from fileengine import ROOT_UID

MB = 1024 * 1024


def _pipeline(mf, store):
    return ConversionPipeline(mf=mf, store=store)  # default registry (text plugin)


# --- the pipeline's refusal -------------------------------------------------
def test_an_oversized_file_is_refused_without_reading_it():
    mf = FakeMF()
    # Claims 40 MB while carrying almost nothing: the point is that the refusal
    # is decided from the stat, so the bytes never have to exist.
    mf.add_file("big", "huge.pdf", content=b"x", version="v1", size=40 * MB)
    store = FakeStore()

    out = _pipeline(mf, store).convert("big", "default", max_bytes=12 * MB)

    assert out.status == "skipped"
    assert out.detail.startswith("too-large")
    # The read is what kills the worker, so this is the assertion that matters.
    assert mf.gets == []


def test_the_refusal_leaves_the_document_exactly_as_it_was():
    """No 'unsupported', no 'error'. Neither is true — the file is fine and may
    well have a converter; it is the limit in force today that refused it. Left
    alone, the first sweep with a bigger limit picks it up with no other
    intervention."""
    mf = FakeMF()
    mf.add_file("big", "huge.pdf", content=b"x", version="v1", size=40 * MB)
    store = FakeStore()

    _pipeline(mf, store).convert("big", "default", max_bytes=12 * MB)

    assert store.upserts == []
    assert ("default", "big") not in store.docs


def test_a_file_at_the_limit_still_converts():
    mf = FakeMF()
    mf.add_file("edge", "notes.txt", content=b"hello", version="v1", size=12 * MB)
    out = _pipeline(mf, FakeStore()).convert("edge", "default", max_bytes=12 * MB)
    assert out.status == "converted"


def test_no_limit_means_no_limit():
    """The on-demand path passes nothing, and must stay able to convert anything
    — somebody is waiting on that specific file, and it is not a crash loop."""
    mf = FakeMF()
    mf.add_file("big", "huge.txt", content=b"hello", version="v1", size=500 * MB)

    assert _pipeline(mf, FakeStore()).convert("big", "default").status == "converted"
    assert _pipeline(mf, FakeStore()).convert("big", "default", max_bytes=0).status == "converted"


# --- the sweep ---------------------------------------------------------------
def _row(**kw):
    base = dict(file_uid="f1", status="converting", mime="text/plain", name="n.txt",
                source_version="v1", chunks=0)
    base.update(kw)
    return DocRow(**base)


class RowStore:
    def __init__(self, rows): self._rows = rows
    def list_documents(self, tenant, **kw): return self._rows


def test_the_sweep_gets_past_the_file_that_was_killing_it():
    """The production shape: a document stuck at 'converting' because the last
    run died on it, with real work queued behind it. The sweep must skip it and
    finish, not stop there."""
    mf = FakeMF()
    mf.add_file("big", "huge.pdf", content=b"x", version="v1", size=40 * MB)
    mf.add_file("small", "notes.txt", content=b"hello", version="v1")
    store = FakeStore()
    pipe = _pipeline(mf, store)
    rows = [_row(file_uid="big", name="huge.pdf"), _row(file_uid="small")]

    counts = sweep_tenant(RowStore(rows), pipe.registry, pipe, "default",
                          max_bytes=12 * MB)

    assert counts["too_large"] == 1
    assert counts["converted"] == 1          # the work behind it was reached
    assert mf.gets == ["small"]              # and only its content was read


def test_without_a_limit_the_sweep_would_have_read_the_whole_thing():
    """The before-picture, so the guard above is testing something."""
    mf = FakeMF()
    mf.add_file("big", "huge.pdf", content=b"x", version="v1", size=40 * MB)
    store = FakeStore()
    pipe = _pipeline(mf, store)

    counts = sweep_tenant(RowStore([_row(file_uid="big", name="huge.pdf")]),
                          pipe.registry, pipe, "default")

    assert counts.get("too_large", 0) == 0
    assert mf.gets == ["big"]


def test_sweep_defaults_the_limit_from_config(monkeypatch):
    """Every caller of sweep() is an unattended pass — the startup thread and the
    admin endpoint alike — so the limit has to be the default rather than
    something each one remembers to ask for."""
    import convert_search_ai.core_client as cc
    import convert_search_ai.indexing as ix
    import convert_search_ai.pipeline as pl
    import convert_search_ai.reconcile as recon
    import convert_search_ai.store as st

    seen = {}

    def fake_sweep_tenant(store, registry, pipeline, tenant, **kw):
        seen.update(kw)
        return {}

    # sweep() assembles a real client, store and pipeline before it sweeps.
    monkeypatch.setattr(recon, "sweep_tenant", fake_sweep_tenant)
    monkeypatch.setattr(cc, "agent_client", lambda cfg: FakeMF())
    monkeypatch.setattr(ix, "Indexer", lambda cfg: None)
    monkeypatch.setattr(st, "DocumentStore", lambda cfg: FakeStore())
    monkeypatch.setattr(pl, "ConversionPipeline",
                        lambda **kw: type("P", (), {"registry": None})())

    class Cfg:
        tenant = "default"
        reconcile_max_bytes = 7 * MB

    recon.sweep(Cfg())
    assert seen["max_bytes"] == 7 * MB

    # 0 means "no limit", and must reach the sweep as None rather than as 0 —
    # they behave the same downstream, but only one of them says so.
    seen.clear()
    Cfg.reconcile_max_bytes = 0
    recon.sweep(Cfg())
    assert seen["max_bytes"] is None


# --- the tree walk -----------------------------------------------------------
def test_the_walk_refuses_from_the_listing_without_even_a_stat():
    """The listing already carries the size, so an oversized file costs nothing
    on the walk — it never reaches the pipeline at all."""
    mf = FakeMF()
    mf.children[ROOT_UID] = [
        FakeEntry("big", "huge.pdf", size=40 * MB),
        FakeEntry("small", "notes.txt", size=11),
    ]
    mf.add_file("big", "huge.pdf", content=b"x", version="v1", size=40 * MB)
    mf.add_file("small", "notes.txt", content=b"hello world", version="v1")
    store = FakeStore()
    pipe = _pipeline(mf, store)

    counts = reconcile_tenant(mf, pipe, "default", max_bytes=12 * MB)

    assert counts["too_large"] == 1
    assert counts["converted"] == 1
    assert mf.gets == ["small"]


def test_the_walk_is_unbounded_when_no_limit_is_given():
    mf = FakeMF()
    mf.children[ROOT_UID] = [
        FakeEntry("big", "huge.txt", size=40 * MB),
    ]
    mf.add_file("big", "huge.txt", content=b"hello", version="v1", size=40 * MB)
    pipe = _pipeline(mf, FakeStore())

    counts = reconcile_tenant(mf, pipe, "default")

    assert counts["too_large"] == 0
    assert counts["converted"] == 1


# --- the knob ----------------------------------------------------------------
def test_the_default_limit_is_12_mib(monkeypatch):
    monkeypatch.delenv("CSAI_RECONCILE_MAX_BYTES", raising=False)
    from convert_search_ai.config import Config
    assert Config().reconcile_max_bytes == 12 * MB


def test_the_limit_is_configurable_and_can_be_turned_off(monkeypatch):
    from convert_search_ai.config import Config

    monkeypatch.setenv("CSAI_RECONCILE_MAX_BYTES", str(50 * MB))
    assert Config().reconcile_max_bytes == 50 * MB

    monkeypatch.setenv("CSAI_RECONCILE_MAX_BYTES", "0")
    assert Config().reconcile_max_bytes == 0  # 0 = no limit


# --- the OTHER startup loop --------------------------------------------------
class ReplaySource:
    """Just enough of the Redis stream source for the PEL replay."""
    def __init__(self, entries):
        self.pending = list(entries)
        self.acked = []

    def read_pending(self, count=32):
        return list(self.pending)

    def ack(self, ids):
        self.acked.extend(ids)
        self.pending = [(mid, ev) for (mid, ev) in self.pending if mid not in ids]


def _ingestor(monkeypatch, mf, store, source, limit):
    from convert_search_ai.config import Config
    from convert_search_ai.ingest import Ingestor

    monkeypatch.setenv("CSAI_RECONCILE_MAX_BYTES", str(limit))
    ing = Ingestor(Config(), _pipeline(mf, store), store, source)
    monkeypatch.setattr(ing, "_ensure_tenant", lambda tenant: None)
    monkeypatch.setattr(ing.emitter, "emit_conversion", lambda event, outcome: None)
    return ing


def test_the_startup_replay_is_limited_too(monkeypatch):
    """The loop a sweep limit cannot reach.

    An entry sits un-acked precisely because the worker stopped mid-conversion,
    which is exactly what being killed by a large document looks like.
    drain_pending replays it on every start — BEFORE the event loop and before
    the sweep — so without a limit here the process dies in the same place every
    time and the sweep's limit is never even consulted."""
    mf = FakeMF()
    mf.add_file("big", "huge.pdf", content=b"x", version="v1", size=40 * MB)
    store = FakeStore()
    src = ReplaySource([("1-1", {"type": "file.created", "file_uid": "big",
                                 "tenant": "default"})])
    ing = _ingestor(monkeypatch, mf, store, src, 12 * MB)

    assert ing.drain_pending() == 1
    assert mf.gets == []            # never read, so the worker survives it
    assert src.acked == ["1-1"]     # and the entry stops coming back


def test_a_live_event_is_not_size_limited(monkeypatch):
    """Deliberate: an upload gets its preview however big it is. The limit is for
    unattended catch-up work, not for the thing somebody just did."""
    mf = FakeMF()
    mf.add_file("big", "huge.txt", content=b"hello", version="v1", size=40 * MB)
    store = FakeStore()
    ing = _ingestor(monkeypatch, mf, store, ReplaySource([]), 12 * MB)

    ing.handle({"type": "file.created", "file_uid": "big", "tenant": "default"})

    assert mf.gets == ["big"]


# --- which types the limit is for --------------------------------------------
#
# Not "is it big" — where its memory goes. A converter that hands a temp file to
# ffmpeg or ImageMagick expands in a child process; one that parses in this
# interpreter (docling, pypdf) expands here, and that is what kills the worker.
# A plugin that enforces a ceiling of its own is deferred to rather than
# second-guessed.
def _default_registry():
    from convert_search_ai.plugins.registry import default_registry
    return default_registry(None)


def test_in_process_converters_are_the_limited_ones():
    reg = _default_registry()
    for mime in ("application/pdf",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "text/plain"):
        assert reg.bounds_own_memory(mime) is False, mime


def test_streaming_and_self_limiting_converters_are_not():
    reg = _default_registry()
    # ffmpeg and ImageMagick: a child process with its own limits.
    assert reg.bounds_own_memory("video/mp4") is True
    assert reg.bounds_own_memory("image/jpeg") is True
    # 3D defers to its own threed_max_input_mb (512 MB), chosen for what BIM
    # models actually weigh — the blanket limit would refuse ordinary ones.
    assert reg.bounds_own_memory("application/x-ifc") is True


def test_an_unclaimed_type_is_limited():
    """An extension we cannot place is the case where reading the whole file is
    precisely the risk, so it gets the limit rather than the benefit of doubt."""
    assert _default_registry().bounds_own_memory("application/octet-stream") is False


def test_a_large_video_is_still_converted():
    mf = FakeMF()
    mf.add_file("clip", "lecture.mp4", content=b"x", version="v1", size=900 * MB)

    out = _pipeline(mf, FakeStore()).convert("clip", "default", max_bytes=12 * MB)

    assert out.detail != "too-large"
    assert mf.gets == ["clip"]          # read, i.e. not refused on size


def test_a_large_bim_model_is_still_converted():
    mf = FakeMF()
    mf.add_file("m", "tower.ifc", content=b"x", version="v1", size=200 * MB)

    _pipeline(mf, FakeStore()).convert("m", "default", max_bytes=12 * MB)

    assert mf.gets == ["m"]


def test_a_large_office_document_is_refused():
    mf = FakeMF()
    mf.add_file("d", "report.docx", content=b"x", version="v1", size=40 * MB)

    out = _pipeline(mf, FakeStore()).convert("d", "default", max_bytes=12 * MB)

    assert out.detail.startswith("too-large")
    assert mf.gets == []


def test_the_walk_applies_the_same_rule():
    mf = FakeMF()
    mf.children[ROOT_UID] = [
        FakeEntry("clip", "lecture.mp4", size=900 * MB),   # streams — converted
        FakeEntry("doc", "report.pdf", size=40 * MB),      # in-process — refused
    ]
    mf.add_file("clip", "lecture.mp4", content=b"x", version="v1", size=900 * MB)
    mf.add_file("doc", "report.pdf", content=b"x", version="v1", size=40 * MB)
    pipe = _pipeline(mf, FakeStore())

    counts = reconcile_tenant(mf, pipe, "default", max_bytes=12 * MB)

    assert counts["too_large"] == 1
    assert mf.gets == ["clip"]

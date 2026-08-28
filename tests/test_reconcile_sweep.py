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

"""The reconcile sweep's selection rule — which documents get retried and which
are correctly left alone."""
from convert_search_ai.plugins.base import ConversionPlugin, Rendition
from convert_search_ai.plugins.registry import PluginRegistry
from convert_search_ai.reconcile import RETRY_STATUSES, needs_conversion, sweep_tenant
from convert_search_ai.store import DocRow


class TextPlugin(ConversionPlugin):
    """Extracts text, like the real text/office/pdf plugins."""
    name = "text"
    def supports(self, mime): return mime == "text/plain"
    def extract(self, data, mime, name): return "hello"


class RenderOnlyPlugin(ConversionPlugin):
    """Renders but never extracts — an image or video converter."""
    name = "img"
    def supports(self, mime): return mime == "image/heic"
    def render(self, data, mime, name):
        return [Rendition("thumbnail", "png", b"PNG", "image/png")]


def row(**kw):
    base = dict(file_uid="f1", status="indexed", mime="text/plain", name="n.txt",
                source_version="v1", chunks=3)
    base.update(kw)
    return DocRow(**base)


# --- reason 1: the run did not finish ---------------------------------------
def test_every_unfinished_status_is_retried():
    reg = PluginRegistry([TextPlugin()])
    for status in RETRY_STATUSES:
        assert needs_conversion(row(status=status), reg) == status


def test_converting_is_retried_because_it_means_a_crashed_run():
    # The exact shape of the production fault: the pipeline writes 'converting'
    # before doing the work, so a row still holding it never came back.
    reg = PluginRegistry([TextPlugin()])
    assert needs_conversion(row(status="converting", chunks=0), reg) == "converting"


# --- reason 2: coverage grew ------------------------------------------------
def test_unsupported_becomes_retryable_once_a_plugin_claims_the_type():
    stale = row(status="unsupported", mime="text/plain", chunks=0)
    assert needs_conversion(stale, PluginRegistry([])) is None
    assert needs_conversion(stale, PluginRegistry([TextPlugin()])) == "unsupported/now-supported"


def test_a_render_only_plugin_also_revives_its_unsupported_files():
    """The image case: new support that produces renditions and NO text at all.
    A text-only test would skip exactly the files the new plugin was added for."""
    heic = row(status="unsupported", mime="image/heic", chunks=0)
    assert needs_conversion(heic, PluginRegistry([])) is None
    assert needs_conversion(heic, PluginRegistry([RenderOnlyPlugin()])) == "unsupported/now-supported"


def test_unsupported_stays_unsupported_when_nothing_claims_the_type():
    junk = row(status="unsupported", mime="application/octet-stream", chunks=0)
    reg = PluginRegistry([TextPlugin(), RenderOnlyPlugin()])
    assert needs_conversion(junk, reg) is None


# --- reason 3: text that never landed ---------------------------------------
def test_no_chunks_with_a_text_extractor_is_a_failed_extraction():
    reg = PluginRegistry([TextPlugin()])
    assert needs_conversion(row(status="converted", chunks=0), reg) == "converted/no-chunks"


def test_images_are_not_swept_forever_just_because_they_have_no_chunks():
    """A JPEG legitimately has no text. Re-converting it on every sweep would be
    waste, and it is the difference between a sweep that settles and one that
    never stops doing work."""
    reg = PluginRegistry([RenderOnlyPlugin()])
    img = row(status="converted", mime="image/heic", chunks=0)
    assert needs_conversion(img, reg) is None


def test_healthy_indexed_document_is_left_alone():
    reg = PluginRegistry([TextPlugin()])
    assert needs_conversion(row(status="indexed", chunks=5), reg) is None


# --- the sweep loop ---------------------------------------------------------
class FakeStore:
    def __init__(self, rows): self._rows = rows
    def list_documents(self, tenant, **kw): return self._rows


class FakePipeline:
    def __init__(self, statuses=None):
        self.calls = []
        self._statuses = statuses or {}
    def convert(self, uid, tenant, force=False):
        self.calls.append((uid, force))
        class Out: pass
        o = Out(); o.status = self._statuses.get(uid, "indexed")
        return o


def test_sweep_retries_only_what_needs_it_and_forces_past_the_idempotency_guard():
    rows = [row(file_uid="ok", status="indexed", chunks=4),
            row(file_uid="stuck", status="converting", chunks=0),
            row(file_uid="img", status="converted", mime="image/heic", chunks=0)]
    pipe = FakePipeline()
    counts = sweep_tenant(FakeStore(rows), PluginRegistry([TextPlugin(), RenderOnlyPlugin()]),
                          pipe, "default")

    assert [c[0] for c in pipe.calls] == ["stuck"]
    # force=True: a row the guard would skip is precisely what the sweep is for.
    assert pipe.calls[0][1] is True
    assert counts["examined"] == 3 and counts["retried"] == 1 and counts["skipped"] == 2


def test_a_failing_convert_does_not_abort_the_sweep():
    class Boom(FakePipeline):
        def convert(self, uid, tenant, force=False):
            if uid == "bad":
                raise RuntimeError("converter exploded")
            return super().convert(uid, tenant, force)

    rows = [row(file_uid="bad", status="error", chunks=0),
            row(file_uid="good", status="error", chunks=0)]
    pipe = Boom()
    counts = sweep_tenant(FakeStore(rows), PluginRegistry([TextPlugin()]), pipe, "default")

    assert counts["error"] == 1 and counts["retried"] == 1


def test_max_files_bounds_the_work():
    rows = [row(file_uid=f"f{i}", status="error", chunks=0) for i in range(5)]
    pipe = FakePipeline()
    counts = sweep_tenant(FakeStore(rows), PluginRegistry([TextPlugin()]), pipe,
                          "default", max_files=2)
    assert counts["retried"] == 2 and len(pipe.calls) == 2

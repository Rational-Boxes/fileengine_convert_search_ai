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
    # The NAME has to imply nothing either. The default fixture name is "n.txt",
    # which today's detection reads as text/plain — a type TextPlugin claims — so
    # the row would be retried for that reason and this test would no longer be
    # about "nothing claims the type" at all. An extension-less blob is the case
    # it means: unknown bytes, unknown name, nothing to offer it to.
    junk = row(status="unsupported", mime="application/octet-stream",
               name="blob", chunks=0)
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
    def convert(self, uid, tenant, force=False, max_bytes=None):
        self.calls.append((uid, force))
        class Out: pass
        o = Out(); o.status = self._statuses.get(uid, "indexed"); o.detail = ""
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
        def convert(self, uid, tenant, force=False, max_bytes=None):
            if uid == "bad":
                raise RuntimeError("converter exploded")
            return super().convert(uid, tenant, force, max_bytes)

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


# --- reason 4: a generic verdict that now resolves elsewhere -----------------
#
# This is how a detection fix reaches the files it was written for. Markdown was
# recorded `text/plain` and converted by the source-code formatter, at status
# 'converted' — a state no status-based rule revisits. Detection now refines the
# name, so the sweep can see that the recorded type is not what this file is.

from convert_search_ai.plugins.registry import default_registry  # noqa: E402


def test_markdown_recorded_as_plain_text_is_reconverted():
    r = default_registry(None)
    why = needs_conversion(row(status="indexed", mime="text/plain",
                               name="notes.md", chunks=5), r)
    assert why == "mistyped/text/plain->text/markdown"


def test_and_it_does_not_fire_again_once_the_type_is_recorded_properly():
    # Self-extinguishing: the re-conversion stores text/markdown, which is not a
    # generic type, so the row cannot match this rule a second time. Without that
    # property the sweep would re-convert the same file on every pass forever.
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="text/markdown",
                                name="notes.md", chunks=5), r) is None


def test_a_file_whose_bytes_really_identified_it_is_left_alone():
    # A PNG called notes.txt has a SPECIFIC recorded type — the content answered,
    # and the name must not drag it back into a re-convert loop.
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="image/png",
                                name="notes.txt", chunks=0), r) is None


def test_plain_text_is_not_reconverted_just_for_being_generic():
    # text/plain for a .txt file is the right answer, and the same plugin handles
    # it either way — nothing to redo.
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="text/plain",
                                name="notes.txt", chunks=2), r) is None


def test_a_row_with_no_recorded_name_is_left_alone():
    # Older rows may have no name. Guessing from an empty string would either do
    # nothing or do something arbitrary; leaving it alone is the honest option.
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="text/plain",
                                name="", chunks=1), r) is None


def test_an_obj_model_recorded_as_plain_text_is_reconverted():
    # Same defect, quieter: OBJ has no sniffable signature, so a .obj model was
    # plain text and the 3D chain never ran on it.
    r = default_registry(None)
    why = needs_conversion(row(status="converted", mime="text/plain",
                               name="model.obj", chunks=0), r)
    assert why == "mistyped/text/plain->model/obj"


def test_markdown_recorded_as_a_language_guess_is_also_reconverted():
    # The 227-file case: libmagic called the fenced code blocks javascript, so the
    # record is specific and a generic-only rule would never revisit it.
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="application/javascript",
                                name="notes.md", chunks=9), r) \
        == "mistyped/application/javascript->text/markdown"
    assert needs_conversion(row(status="indexed", mime="text/html",
                                name="notes.md", chunks=9), r) \
        == "mistyped/text/html->text/markdown"


def test_a_source_file_is_never_swept_for_its_subtype():
    # text/x-script.python (libmagic) vs text/x-python (mimetypes) is a difference
    # that means nothing. Driving the rule off the curated maps is what stops this
    # re-converting every source file on every sweep, forever.
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="text/x-script.python",
                                name="script.py", chunks=4), r) is None
    assert needs_conversion(row(status="indexed", mime="text/html",
                                name="page.html", chunks=4), r) is None


def test_a_real_format_record_is_left_alone_whatever_the_name_says():
    r = default_registry(None)
    assert needs_conversion(row(status="indexed", mime="application/pdf",
                                name="notes.md", chunks=4), r) is None

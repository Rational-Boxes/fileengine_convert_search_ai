"""The marked-up PDF (EXTRACTION_PROMPT_SETS.md §5) against real PDFs.

The quotes come from the stored Markdown, so they differ from the PDF text in
the ways a datasheet's conversion makes them differ: table pipes, wrapping,
hyphenation, ligatures, typographic dashes. Each case pins which method finds
it; the absent case pins that nothing is drawn and it is reported."""
import pymupdf
import pytest

from convert_search_ai.headless import markup as mk


def pdf(*pages):
    """Each page: a list of (x, y, text) placed exactly, so lines and table
    cells are where a real datasheet puts them."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page(width=595, height=842)
        for x, y, text in items:
            page.insert_text((x, y), text, fontsize=11, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def opened(data):
    return pymupdf.open(stream=data, filetype="pdf")


DATASHEET = pdf(
    [(50, 80, "Door D-90 technical data"),
     (50, 120, "Fire resistance"), (300, 120, "EI 60"),          # a table row: two cells
     (50, 140, "Leaf width"), (300, 140, "926 mm"),
     (50, 180, "The door leaf is certified to the European fire"),
     (50, 194, "classification standard EN 13501-2 for doors."),  # wraps
     (50, 230, "Manufactured from sustainably sourced tim-"),
     (50, 244, "ber with an FSC certificate.")],                 # hyphenated
    [(50, 80, "Appendix: Fire resistance"), (300, 80, "EI 30 (glazed variant)")],
)


@pytest.mark.parametrize("quote,methods", [
    ("Door D-90 technical data", {"exact"}),
    ("Door D–90 technical data", {"exact"}),                  # en dash in the Markdown
    # PyMuPDF's search spans a line break itself; the word matcher would too.
    ("certified to the European fire classification standard EN 13501-2", {"exact", "words"}),
    ("sustainably sourced timber with an FSC certificate", {"words"}),   # hyphenated + "."
    ("| Fire resistance | EI 60 |", {"exact", "cells"}),          # a table row on one line
    ("**Leaf width** | 926 mm", {"exact", "cells"}),
])
def test_locates_on_the_right_page(quote, methods):
    hit = mk.locate(opened(DATASHEET), quote)
    assert hit is not None, quote
    assert hit["page"] == 0 and hit["method"] in methods and hit["rects"]


def test_normalise_unifies_ligatures_dashes_quotes_and_markdown():
    # Base-14 fonts cannot draw a ligature glyph into a test PDF, so the
    # ligature case is pinned on the normaliser that both sides go through.
    assert mk.normalise("**Surface ﬁnish** – “matt” | EI 60") == \
        'surface finish - "matt" ei 60'
    assert mk._tokens("FSC certificate. (EN 13501-2)") == ["fsc", "certificate", "en", "13501-2"]


def test_an_absent_quote_is_not_located():
    assert mk.locate(opened(DATASHEET), "Acoustic rating 32 dB") is None


def test_the_offset_prefers_the_nearer_occurrence():
    two = pdf([(50, 80, "Thermal transmittance 1.1")], [(50, 80, "Thermal transmittance 1.1")])
    doc = opened(two)
    assert mk.locate(doc, "Thermal transmittance 1.1", ratio=0.1)["page"] == 0
    assert mk.locate(doc, "Thermal transmittance 1.1", ratio=0.9)["page"] == 1


def test_cells_must_be_on_one_line():
    apart = pdf([(50, 100, "Fire resistance"), (300, 400, "EI 60")])
    assert mk.locate(opened(apart), "| Fire resistance | EI 60 |") is None


FIELDS = [{"name": "fire_rating", "label": "Fire rating"}, {"name": "width"},
          {"name": "acoustic"}, {"name": "epd"}]
DATA = {
    "fire_rating": {"found": True, "value": "EI 60", "unit": None,
                    "evidence": {"quote": "| Fire resistance | EI 60 |", "offset": 40}},
    "width": {"found": True, "value": 926, "unit": "mm",
              "evidence": {"quote": "Leaf width | 926 mm", "offset": 70}},
    "acoustic": {"found": True, "value": 32, "unit": "dB",
                 "evidence": {"quote": "Acoustic rating 32 dB", "offset": 90}},
    "epd": {"found": False, "value": None, "unit": None, "evidence": None},
}


def test_mark_up_draws_notes_reports_and_appends_a_legend():
    out, report = mk.mark_up(DATASHEET, DATA, FIELDS, text_len=400, title="Door datasheet",
                             legend={"Model": "fake"}, colours={"width": "#123456"})
    assert report["fire_rating"]["located"] is True and report["fire_rating"]["page"] == 1
    assert report["width"]["located"] is True
    assert report["acoustic"] == {"located": False, "reason": "not_located"}
    assert report["epd"] == {"located": False, "reason": "not_found"}
    doc = opened(out)
    assert len(doc) == 3                                           # 2 pages + legend
    page = doc[0]                      # held: annotations are bound to their page object
    annots = list(page.annots())
    notes = {a.info["content"] for a in annots}
    assert "Fire rating: EI 60" in notes and "width: 926 mm" in notes
    kinds = {a.type[1] for a in annots}
    assert kinds == {"Highlight", "Underline"}                     # evidence + the value itself
    width = [a for a in annots if a.info["content"] == "width: 926 mm" and a.type[1] == "Highlight"][0]
    assert tuple(round(c, 3) for c in width.colors["stroke"]) == \
        tuple(round(c / 255, 3) for c in (0x12, 0x34, 0x56))
    legend = doc[2].get_text()
    assert "Door datasheet" in legend and "Model: fake" in legend
    assert "not stated in the document" in legend
    assert "found in the text, not located on a page" in legend
    assert "page 1" in legend


def test_the_source_is_not_modified():
    before = DATASHEET
    mk.mark_up(DATASHEET, DATA, FIELDS, text_len=400, title="t", legend={})
    assert DATASHEET == before and len(opened(DATASHEET)) == 2


# ------------------------------------------------------------------ MarkupStore

class FakeFiles:
    """The ManagedFiles calls MarkupStore makes."""
    def __init__(self, files, children=None):
        self.files = files                     # uid -> bytes
        self.children = children or {}         # parent uid -> [(uid, name)]
        self.put = {}

    def get_stream(self, uid, version="", tenant=None):
        data = self.files[uid]
        for i in range(0, len(data), 4):
            yield data[i:i + 4]

    def dir(self, uid, tenant=None):
        from types import SimpleNamespace as NS
        return [NS(uid=u, name=n) for u, n in self.children.get(uid, [])]

    def touch(self, parent, name, tenant=None):
        uid = f"new-{name}"
        self.children.setdefault(parent, []).append((uid, name))
        return uid

    def put_stream(self, uid, chunks, tenant=None):
        self.put[uid] = b"".join(chunks)


def test_the_source_is_the_pdf_itself_or_the_pdf_rendition():
    fs = FakeFiles({"f": b"%PDF-source", "r": b"%PDF-rendition"},
                   {"d": [("r", "v1-pdf.pdf"), ("x", "v1-thumbnail.png")]})
    st = mk.MarkupStore(lambda: fs, max_bytes=1000)
    assert st.source("f", "v1", "Sheet.PDF", "t") == (b"%PDF-source", "")
    assert st.source("d", "v1", "spec.docx", "t") == (b"%PDF-rendition", "")
    assert st.source("d", "v2", "spec.docx", "t") == (None, "no_pdf")     # no rendition of v2
    assert st.source("e", "v1", "notes.odt", "t") == (None, "no_pdf")


def test_an_oversized_pdf_is_not_read_whole():
    fs = FakeFiles({"f": b"x" * 100})
    assert mk.MarkupStore(lambda: fs, max_bytes=10).source("f", "v1", "a.pdf", "t") == \
        (None, "too_large")


def test_write_creates_then_replaces_the_sidecar():
    fs = FakeFiles({})
    st = mk.MarkupStore(lambda: fs, max_bytes=10)
    uid, name = st.write("f", "20261102_101010.123", "extractpdf_abc", b"one", "t")
    assert name == "20261102_101010.123-extractpdf_abc.pdf" and fs.put[uid] == b"one"
    assert st.existing("f", "20261102_101010.123", "extractpdf_abc", "t") == (uid, name)
    uid2, _ = st.write("f", "20261102_101010.123", "extractpdf_abc", b"two", "t")
    assert uid2 == uid and fs.put[uid] == b"two"                     # a new version, same file

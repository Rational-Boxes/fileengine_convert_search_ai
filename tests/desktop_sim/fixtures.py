"""Fixture documents.

Live tier: PDFs with real tables (what Reviera's datasheets and certificates
look like after conversion), and a note that states none of the values.

Sandbox tier: Markdown with labelled rows the stub model reads, carrying the
``STUB-*`` directives that script its behaviour (see ``stub_model``)."""
from __future__ import annotations

import io


def pdf(title: str, rows: list, extra: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    st = getSampleStyleSheet()
    t = Table(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
    doc.build([Paragraph(title, st["Title"]), Paragraph(extra, st["Normal"]), Spacer(1, 12), t])
    return buf.getvalue()


# ------------------------------------------------------------------ live

def door_datasheet(fire: str = "EI 60") -> bytes:
    return pdf("Door D-90 Technical Datasheet", [
        ["Property", "Value"], ["Manufacturer", "Nordwood Doors AB"],
        ["Model", "D-90 Acoustic"], ["Fire resistance", fire],
        ["Width", "926 mm"], ["EPD number", "S-P-04512"]],
        "Internal timber door leaf for offices and schools.")


def fsc_certificate() -> bytes:
    return pdf("FSC Chain of Custody Certificate", [
        ["Certificate holder", "Nordwood Doors AB"],
        ["Certificate code", "SA-COC-012345"], ["Valid until", "2028-03-31"]],
        "This certificate confirms the chain of custody of the holder's products.")


def two_model_datasheet() -> bytes:
    return pdf("Door range D-90 / D-120 Datasheet", [
        ["Property", "D-90", "D-120"], ["Manufacturer", "Nordwood Doors AB", "Nordwood Doors AB"],
        ["Fire resistance", "EI 60", "EI 120"], ["Width", "926 mm", "1026 mm"]],
        "Two door models with different fire ratings.")


def window_datasheet() -> bytes:
    return pdf("Window W-12 Datasheet", [
        ["Property", "Value"], ["Manufacturer", "Klarvy Fönster AB"],
        ["U-value", "0.9 W/m2K"], ["Width", "1200 mm"]],
        "Triple glazed timber-aluminium window.")


NOTE = b"Meeting notes. We discussed the site schedule and lunch options.\n"

LIVE_SCHEMA = {"type": "object", "properties": {
    "manufacturer": {"type": "string"},
    "fire_rating": {"type": "string", "description": "fire resistance class, e.g. EI 30"},
    "width_mm": {"type": "number"},
    "epd_number": {"type": "string"},
    "certificate_code": {"type": "string"},
    "valid_until": {"type": "string", "description": "ISO date"}}}


# ------------------------------------------------------------------ sandbox

def md(title: str, rows: dict, *directives: str, body: str = "") -> bytes:
    lines = [f"# {title}", "", body, "", "| Property | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows.items()]
    lines += [""] + list(directives) + [""]
    return "\n".join(lines).encode()


SANDBOX_SCHEMA = {"type": "object", "properties": {
    "manufacturer": {"type": "string"},
    "fire_rating": {"type": "string"},
    "width_mm": {"type": "number"},
    "epd_number": {"type": "string"}}}

GOOD_ROWS = {"Manufacturer": "Nordwood Doors AB", "Fire rating": "EI 60",
             "Width mm": "926", "EPD number": "S-P-04512"}


def long_document(rows: dict, filler_chars: int) -> bytes:
    """A document whose labelled rows sit after ``filler_chars`` of prose, so a
    small extraction window puts them in a later part."""
    para = ("This section describes installation, handling and maintenance of the "
            "product in general terms without stating any of its properties. ")
    filler = "\n\n".join(para * 3 for _ in range(max(1, filler_chars // (len(para) * 3))))
    return md("Long manual", rows, body=filler)

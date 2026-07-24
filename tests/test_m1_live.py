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

"""M1 exit-criteria integration test (``@live``): dropping a file results in
correct hidden-child renditions, idempotently — exercised against the real gRPC
core. Uses a fake store (no Postgres needed) and a deterministic one-rendition
plugin so the test doesn't depend on LibreOffice/ImageMagick being installed."""
import time

import pytest

from conftest import live
from convert_search_ai import tools
from convert_search_ai.pipeline import ConversionPipeline
from convert_search_ai.plugins.base import ConversionPlugin, Rendition
from convert_search_ai.plugins.registry import PluginRegistry, default_registry
from fakes import FakeStore


class OneRendition(ConversionPlugin):
    name = "one"

    def supports(self, mime):
        return True

    def render(self, data, mime, name):
        return [Rendition("thumbnail", "png", b"\x89PNG\r\n\x1a\nFAKE", "image/png")]

    def extract(self, data, mime, name):
        return "extracted text content"


@live
def test_rendition_round_trip(config):
    from convert_search_ai.core_client import agent_client

    mf = agent_client(config)
    tenant = config.tenant
    workdir = f"csai_m1_{int(time.time())}"

    dir_uid = mf.mkdir("", workdir, tenant=tenant)   # under root (agent is system_admin)
    assert dir_uid, "could not create working directory under root"
    file_uid = mf.touch(dir_uid, "doc.dat", tenant=tenant)
    assert file_uid
    assert mf.put(file_uid, b"some bytes to convert", tenant=tenant) is not False

    try:
        pipeline = ConversionPipeline(
            mf=mf, store=FakeStore(), registry=PluginRegistry([OneRendition()])
        )

        # First conversion: writes the rendition as a hidden child.
        out = pipeline.convert(file_uid, tenant)
        assert out.status == "converted"
        assert out.has_markdown is True
        assert len(out.renditions_written) == 1
        rend_name = out.renditions_written[0]

        names = {e.name for e in (mf.dir(file_uid, tenant=tenant) or [])}
        assert rend_name in names, f"rendition {rend_name} not found in {names}"

        # Second conversion of the same version: idempotent — no new rendition.
        again = pipeline.convert(file_uid, tenant)
        assert again.renditions_written == []
        names2 = {e.name for e in (mf.dir(file_uid, tenant=tenant) or [])}
        assert names2 == names
    finally:
        mf.remove(file_uid, tenant=tenant)
        mf.remove(dir_uid, tenant=tenant)
        mf.close()


@live
@pytest.mark.skipif(not tools.have("pdftoppm"), reason="pdftoppm (poppler) not installed")
def test_document_previews_written_via_live_core(config):
    """The real default registry writes the document preview set (icon-sized
    thumbnail + larger first-page preview) as hidden children of a PDF, through
    the live gRPC core — idempotently. Complements the fake-plugin round-trip
    above by exercising the actual poppler-backed doc_preview path."""
    fpdf = pytest.importorskip("fpdf")
    from convert_search_ai.core_client import agent_client

    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.cell(0, 10, "Live Rendition Pipeline Check")
    data = bytes(pdf.output())

    mf = agent_client(config)
    tenant = config.tenant
    dir_uid = mf.mkdir("", f"csai_docprev_{int(time.time())}", tenant=tenant)
    file_uid = mf.touch(dir_uid, "report.pdf", tenant=tenant)
    assert file_uid and mf.put(file_uid, data, tenant=tenant) is not False

    try:
        pipeline = ConversionPipeline(
            mf=mf, store=FakeStore(), registry=default_registry(config)
        )
        out = pipeline.convert(file_uid, tenant)
        assert out.status == "converted"

        names = {e.name for e in (mf.dir(file_uid, tenant=tenant) or [])}
        assert any(n.endswith("-thumbnail.png") for n in names), names
        assert any(n.endswith("-preview.png") for n in names), names

        # Idempotent: a second conversion of the same version writes nothing new.
        again = pipeline.convert(file_uid, tenant)
        assert again.renditions_written == []
        assert {e.name for e in (mf.dir(file_uid, tenant=tenant) or [])} == names
    finally:
        mf.remove(file_uid, tenant=tenant)
        mf.remove(dir_uid, tenant=tenant)
        mf.close()

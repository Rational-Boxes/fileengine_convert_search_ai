"""M1 exit-criteria integration test (``@live``): dropping a file results in
correct hidden-child renditions, idempotently — exercised against the real gRPC
core. Uses a fake store (no Postgres needed) and a deterministic one-rendition
plugin so the test doesn't depend on LibreOffice/ImageMagick being installed."""
import time

from conftest import live
from convert_search_ai.pipeline import ConversionPipeline
from convert_search_ai.plugins.base import ConversionPlugin, Rendition
from convert_search_ai.plugins.registry import PluginRegistry
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

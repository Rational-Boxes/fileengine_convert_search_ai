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

"""The convertible-formats list the SPA gates "Generate preview" on.

It is derived from the registry, so the property worth pinning is that it says
what dispatch would do — and that it never advertises a preview the deployment
cannot make."""

from convert_search_ai import mime, tools
from convert_search_ai.plugins.registry import conversion_formats, default_registry


def _by_ext(formats):
    return {e: g for g in formats for e in g["extensions"]}


def test_every_listed_extension_is_dispatched_to_the_listed_plugin():
    reg = default_registry()
    for g in conversion_formats(reg):
        for ext in g["extensions"]:
            name = "x." + ext
            assert reg.for_mime(mime.detect(b"", name), name).name == g["plugin"], ext


def test_binaries_and_archives_are_never_listed():
    exts = _by_ext(conversion_formats(default_registry()))
    for ext in ("exe", "bin", "so", "dll", "zip", "tar", "7z", "iso", "msi", "jar", "pyc"):
        assert ext not in exts, ext


def test_common_documents_are_listed_with_their_plugin():
    exts = _by_ext(conversion_formats(default_registry()))
    assert exts["docx"]["plugin"] == "office"
    assert exts["pdf"]["plugin"] == "pdf"
    assert exts["md"]["plugin"] == "markdown"
    assert exts["ifc"]["plugin"] == "model3d"
    assert exts["city.json"]["plugin"] == "model3d"


def test_a_missing_tool_reports_the_format_but_not_a_preview(monkeypatch):
    monkeypatch.setattr(tools, "have", lambda _tool: False)
    exts = _by_ext(conversion_formats(default_registry()))
    # Still claimed (and still indexed for search), so it is reported...
    assert exts["docx"]["renders"] and exts["docx"]["text"]
    # ...but nothing would render it here, so no preview is promised.
    assert not exts["docx"]["preview"]
    assert not exts["mp4"]["preview"]
    assert not exts["png"]["preview"]
    assert not exts["ifc"]["preview"]
    # Pure-Python renderers do not depend on a tool.
    assert exts["py"]["preview"]


def test_occt_formats_need_drawexe_but_ifc_does_not(monkeypatch):
    monkeypatch.setattr(tools, "have", lambda t: t == "convert2xkt")
    exts = _by_ext(conversion_formats(default_registry()))
    assert exts["ifc"]["preview"]        # web-ifc fallback inside convert2xkt
    assert not exts["step"]["preview"]   # needs DRAWEXE as well


def test_a_text_only_plugin_is_not_a_preview():
    from convert_search_ai.plugins.registry import PluginRegistry
    from convert_search_ai.plugins.text import TextMarkdownPlugin
    exts = _by_ext(conversion_formats(PluginRegistry([TextMarkdownPlugin()])))
    assert exts["txt"]["text"] and not exts["txt"]["renders"] and not exts["txt"]["preview"]

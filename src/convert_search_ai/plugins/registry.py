"""Plugin registry + dispatch."""
from __future__ import annotations

from typing import List, Optional

from .base import ConversionPlugin, ConversionResult


class PluginRegistry:
    def __init__(self, plugins: Optional[List[ConversionPlugin]] = None):
        self._plugins: List[ConversionPlugin] = list(plugins or [])

    def register(self, plugin: ConversionPlugin) -> None:
        self._plugins.append(plugin)

    def for_mime(self, mime: str) -> Optional[ConversionPlugin]:
        """First registered plugin that supports ``mime`` (registration order =
        priority — most specific plugins are registered first)."""
        for p in self._plugins:
            try:
                if p.supports(mime):
                    return p
            except Exception:
                continue
        return None

    def convert(self, data: bytes, mime: str, name: str = "") -> ConversionResult:
        """Run the matching plugin. Unknown MIME → ``supported=False`` (not an error).
        A plugin that raises is treated as producing nothing (fail-soft)."""
        plugin = self.for_mime(mime)
        if plugin is None:
            return ConversionResult(supported=False)
        try:
            renditions = plugin.render(data, mime, name) or []
        except Exception:
            renditions = []
        try:
            markdown = plugin.extract(data, mime, name)
        except Exception:
            markdown = None
        return ConversionResult(renditions=renditions, markdown=markdown, supported=True)


def default_registry(config=None) -> PluginRegistry:
    """The standard plugin set. Specific types first; the text catch-all last.

    ``config`` (optional) supplies the PDF/Office extraction backend order via
    ``config.pdf_backends``."""
    from .html import HtmlPlugin
    from .image import ImagePlugin
    from .markdown_preview import MarkdownPlugin
    from .office import OfficePlugin
    from .pdf import PdfPlugin
    from .source_preview import SourcePreviewPlugin
    from .text import TextMarkdownPlugin
    from .video import VideoPlugin
    from .xeokit3d import Xeokit3DPlugin

    from .doc_preview import DEFAULT_PREVIEW_PX, DEFAULT_THUMBNAIL_PX

    backends = getattr(config, "pdf_backends", None) if config is not None else None
    thumb_px = getattr(config, "doc_thumbnail_px", DEFAULT_THUMBNAIL_PX) if config is not None else DEFAULT_THUMBNAIL_PX
    preview_px = getattr(config, "doc_preview_px", DEFAULT_PREVIEW_PX) if config is not None else DEFAULT_PREVIEW_PX
    code_style = getattr(config, "code_preview_style", "default") if config is not None else "default"
    code_max = getattr(config, "code_preview_max_lines", 0) if config is not None else 0
    return PluginRegistry([
        PdfPlugin(backends=backends, thumbnail_px=thumb_px, preview_px=preview_px),
        OfficePlugin(pdf_backends=backends, thumbnail_px=thumb_px, preview_px=preview_px),
        ImagePlugin(),
        VideoPlugin(),
        # 3D / BIM / CAD (IFC, glTF/GLB, CityJSON, LAS/LAZ, STL, PLY, and via
        # OpenCASCADE: STEP, IGES, BREP, OBJ, VRML) → XKT + indexed text.
        Xeokit3DPlugin(config),
        # HTML → a full-document PDF + previews (Chromium/LibreOffice). Registered
        # ahead of the source/text catch-alls so text/html is rendered as a
        # document, not treated as source code. Covers chat-generated reports.
        HtmlPlugin(chromium=getattr(config, "html_chromium", "chromium-browser"),
                   pdf_backends=backends, thumbnail_px=thumb_px, preview_px=preview_px,
                   timeout_s=getattr(config, "html_pdf_timeout_s", 60)),
        # Markdown → a *formatted* PDF + previews (rendered document, not raw
        # source). Registered ahead of the source plugin so .md is claimed here.
        MarkdownPlugin(style=code_style, max_lines=code_max, thumbnail_px=thumb_px, preview_px=preview_px),
        # Source/text preview (colour-coded full-document PDF + first-page PNGs).
        # Registered ahead of the plain-text catch-all so text & source files get
        # previews; the text plugin remains as the final fail-soft text extractor.
        SourcePreviewPlugin(style=code_style, max_lines=code_max, thumbnail_px=thumb_px, preview_px=preview_px),
        TextMarkdownPlugin(),
    ])

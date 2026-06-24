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
    from .image import ImagePlugin
    from .office import OfficePlugin
    from .pdf import PdfPlugin
    from .text import TextMarkdownPlugin
    from .video import VideoPlugin

    backends = getattr(config, "pdf_backends", None) if config is not None else None
    return PluginRegistry([
        PdfPlugin(backends=backends),
        OfficePlugin(pdf_backends=backends),
        ImagePlugin(),
        VideoPlugin(),
        TextMarkdownPlugin(),
    ])

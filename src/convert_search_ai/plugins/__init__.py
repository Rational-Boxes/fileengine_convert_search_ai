"""Conversion plugin framework.

A plugin declares the MIME types it handles and produces, for a source file:
- **renditions** — alternate-format presentation copies (preview image, web PDF,
  thumbnail, video preview) written back as hidden children in FileEngine;
- **extracted Markdown** — the document's text content for search/RAG.

See ``base.ConversionPlugin`` and ``registry.default_registry``."""
from .base import ConversionPlugin, ConversionResult, Rendition
from .registry import PluginRegistry, default_registry

__all__ = [
    "ConversionPlugin", "ConversionResult", "Rendition",
    "PluginRegistry", "default_registry",
]

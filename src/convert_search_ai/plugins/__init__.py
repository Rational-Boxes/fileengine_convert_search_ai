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

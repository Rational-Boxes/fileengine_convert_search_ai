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

"""Pluggable AI providers (DEVELOPMENT_PLAN §7).

Embeddings and chat completion sit behind small interfaces selected by config, so
a deployment chooses concrete providers (Voyage/OpenAI/local for embeddings;
Anthropic Claude for chat) without touching the pipeline. A deterministic offline
``hash`` embedder and an ``echo`` chat provider keep dev/tests dependency-free."""
from .base import ChatProvider, EmbeddingProvider, WebSearchProvider, WebSearchResult
from .factory import make_chat_provider, make_embedding_provider, make_web_search_provider

__all__ = [
    "ChatProvider", "EmbeddingProvider", "WebSearchProvider", "WebSearchResult",
    "make_chat_provider", "make_embedding_provider", "make_web_search_provider",
]

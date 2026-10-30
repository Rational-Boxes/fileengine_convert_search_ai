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

"""The headless API for desktop applications (design_documents/HEADLESS_RESEARCH_API.md).

Desktop applications sign in at the REST door with a generated key and send the
session bearer here. The main mode is structured extraction from documents
(``extract``); reports and questions reuse the chat engine over SSE.

  usage    per-key rate limits and the usage status every response carries
  extract  per-document, evidence-backed extraction from stored Markdown
  cache    the version-keyed extraction cache, a sidecar of the source file
  runs     run records, reviewable in the web application, reached by erasure
  sse      server-sent events over a worker thread, cancelled on disconnect
  routes   the HTTP surface
"""

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

"""convert_search_ai — FileEngine conversion, search, and RAG-chat microservice.

M0 (scaffolding): package layout, environment Config, the LDAP auth + gRPC core
client reused from the FileEngine ecosystem, a FastAPI app with health/readiness,
the Postgres baseline migration, and a pytest harness with ``@live`` gating.

Conversion/extraction (M1), full-text search (M2), and vector RAG chat (M3) are
built on top of this skeleton — see design_documents/DEVELOPMENT_PLAN.md."""

__version__ = "0.3.0"

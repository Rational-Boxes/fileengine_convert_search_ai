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

"""CORS: enabled only when CSAI_CORS_ORIGINS is set; reflects allowed origins."""
from fastapi.testclient import TestClient

from convert_search_ai.app import build_app
from convert_search_ai.config import Config


def _client(origins):
    cfg = Config()
    cfg.cors_origins = origins
    return TestClient(build_app(cfg))


def test_cors_headers_present_for_allowed_origin():
    c = _client(["http://localhost:3000"])
    # Preflight for a credentialed POST /search from the SPA origin.
    r = c.options(
        "/search",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-tenant",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

    # A simple GET also gets the allow-origin header reflected.
    r2 = c.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert r2.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_disabled_by_default():
    c = _client([])
    r = c.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in r.headers

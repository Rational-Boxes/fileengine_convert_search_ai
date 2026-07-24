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

"""Route-scoped monitoring IP allowlist (security review L2).

The unauthenticated /healthz|/readyz|/poolz endpoints may be guarded by
FILEENGINE_MONITORING_ALLOW_IPS. The guard must reject a non-listed client IP
on a monitoring path (403), permit a listed IP, never gate non-monitoring paths
(route-scoped), and be a no-op when unset. TestClient's client host is
"testclient".
"""
import os

from fastapi.testclient import TestClient

from convert_search_ai.app import build_app


def _client(allow):
    if allow is None:
        os.environ.pop("FILEENGINE_MONITORING_ALLOW_IPS", None)
    else:
        os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = allow
    return TestClient(build_app())


def test_blocks_non_listed_ip():
    assert _client("10.9.9.9").get("/healthz").status_code == 403


def test_permits_listed_ip():
    assert _client("testclient").get("/healthz").status_code != 403


def test_is_route_scoped():
    assert _client("10.9.9.9").get("/definitely-not-a-route").status_code != 403


def test_no_allowlist_allows_all():
    assert _client(None).get("/healthz").status_code != 403

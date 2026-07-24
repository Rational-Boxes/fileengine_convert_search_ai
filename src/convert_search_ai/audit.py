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

"""Structured, secret-free audit log of permission-gated access.

One JSON line per access decision: {ts, action, user, tenant, result, ...}. Logs
the *shape and outcome* of access (action, file_uid, counts, allow/deny) — never
query text, document content, passwords, or tokens — so the log is safe to retain
and answers "who accessed which document, and was it permitted?". Ported from the
FileEngine MCP server's audit."""
import json
import logging
import sys
import time

_logger = logging.getLogger("convert_search_ai.audit")
_configured = False


def configure(path: str = "") -> None:
    """Send audit records to ``path`` (a file) or stderr when empty."""
    global _configured
    _logger.setLevel(logging.INFO)
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
    handler = logging.FileHandler(path) if path else logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("audit %(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False
    _configured = True


def record(*, action: str, user: str, tenant: str, result: str, **extra) -> None:
    """Append one audit record. ``result`` is ok | denied | missing | error.

    ``extra`` carries non-sensitive shape (file_uid, counts, capped flags)."""
    if not _configured:
        configure()
    entry = {"ts": round(time.time(), 3), "action": action, "user": user,
             "tenant": tenant, "result": result}
    entry.update(extra)
    _logger.info(json.dumps(entry, separators=(",", ":")))

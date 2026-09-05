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

"""The chat socket's anyio helpers are bound, whatever anyio does to its __init__.

Production, 2026-09-05: research chat answered nothing. The model had been
called and had returned 200 — the failure was afterwards, handing the first
token from the producer thread back to the event loop:

    File "convert_search_ai/api.py", line 401, in produce
      anyio.from_thread.run(send.send, ev)
    AttributeError: module 'anyio' has no attribute 'from_thread'

`import anyio` does not bind its submodules. It appeared to, for as long as
something else in the process imported them first; anyio 4.15 stopped doing so
from its own __init__ and the assumption stopped holding. Both images we had —
the one running and the one before it — carried 4.15, so this was not a
regression from a rebuild; it was a latent import bug waiting for the version
that stopped covering for it.

A unit test cannot catch it by exercising the socket (that needs a model), so it
asserts the thing that actually broke: after importing our module, the
attributes are there and callable.
"""


def test_api_module_binds_the_anyio_helpers_it_calls():
    import anyio

    import convert_search_ai.api  # noqa: F401  (imported for its side effect)

    # These are what api.py reaches for on the chat path. Attribute access on a
    # bare `anyio` is exactly the call that raised in production.
    assert hasattr(anyio, "from_thread"), "anyio.from_thread not bound — chat will 500"
    assert hasattr(anyio, "to_thread"), "anyio.to_thread not bound — chat will 500"
    assert callable(anyio.from_thread.run)
    assert callable(anyio.to_thread.run_sync)


def test_the_imports_are_declared_rather_than_inherited():
    """Not just present — present BECAUSE we asked.

    Another module importing anyio.from_thread first would satisfy the test
    above while leaving us relying on someone else's import again, which is the
    bug. So check the source states it.
    """
    from pathlib import Path

    import convert_search_ai.api as api

    src = Path(api.__file__).read_text(encoding="utf-8")
    assert "import anyio.from_thread" in src
    assert "import anyio.to_thread" in src

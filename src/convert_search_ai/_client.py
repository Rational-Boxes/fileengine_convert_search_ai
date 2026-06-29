"""Locate and import the reused FileEngine Python client (``fileengine``).

Prefers an installed package; otherwise falls back to the sibling
``python_interface`` checkout (override with FILEENGINE_PYTHON_CLIENT). Same
bootstrap strategy as the MCP server."""
import os
import sys


def _ensure_on_path() -> None:
    try:
        import fileengine  # noqa: F401
        return
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("FILEENGINE_PYTHON_CLIENT", ""),
        # convert_search_ai/src/convert_search_ai/ -> ../../../python_interface
        os.path.join(here, "..", "..", "..", "python_interface"),
        os.path.join(here, "..", "..", "..", "..", "python_interface"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "fileengine", "__init__.py")):
            sys.path.insert(0, os.path.abspath(c))
            return
    raise ImportError(
        "Could not import 'fileengine'. Install ../python_interface "
        "(`pip install ../python_interface`) or set FILEENGINE_PYTHON_CLIENT."
    )


_ensure_on_path()

from fileengine import (  # noqa: E402
    ManagedFiles, FileEngineError, NotFoundError, WriteUnavailableError,
)

__all__ = ["ManagedFiles", "FileEngineError", "NotFoundError", "WriteUnavailableError"]

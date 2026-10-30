"""Entry point of the sandbox csai (run by ``sandbox.Sandbox``; not a test).

Same as ``python -m convert_search_ai.app``, with two test-only changes: MCP
endpoints on loopback are accepted, so the stub MCP server can be registered;
and no event consumer runs, so the dev csai keeps all of its events.
Everything else comes from the environment the harness sets (port, stub model,
limits) on top of ``./.env``."""
import logging
import os

from convert_search_ai import mcp_client
from convert_search_ai.app import build_app
from convert_search_ai.config import Config, load_dotenv


def _allow_loopback(url: str) -> str:
    if url.startswith(("http://127.0.0.1:", "http://localhost:")):
        return url
    return _strict(url)


_strict = mcp_client.validate_endpoint
mcp_client.validate_endpoint = _allow_loopback


def main() -> None:
    import uvicorn
    logging.basicConfig(level=os.environ.get("SANDBOX_LOG_LEVEL", "WARNING"))
    load_dotenv()
    # No permission-cache invalidator: it would join the dev csai's consumer
    # group under the same consumer name and take its events.
    app = build_app(Config(), enable_event_invalidation=False)
    cfg = app.state.config
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_level="warning")


if __name__ == "__main__":
    main()

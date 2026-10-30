"""A simulated desktop application driving headless csai end to end.

``app.DesktopApp`` behaves as a desktop client would: it signs in at the REST
door with a generated key:secret, refreshes its session, reads SSE streams
incrementally, answers MCP approval requests, watches the usage status and
backs off on 429, cancels by hanging up, and downloads reports through the REST
door. ``scenarios`` drives it through the headless contract
(HEADLESS_RESEARCH_API.md); ``run`` is the command line.

Two tiers:

* ``live``: the dev stack's csai and its configured chat model. Measures that
  the real model returns correct, evidence-backed values.
* ``sandbox``: a second csai the harness starts against the same core, bridge,
  LDAP, Postgres and Redis, with a scripted OpenAI-compatible model
  (``stub_model``), a stub MCP server (``stub_mcp``) and low limits. Makes the
  protocol paths deterministic: budgets, concurrency, cancellation, approvals,
  invalid model replies, Redis down.
"""

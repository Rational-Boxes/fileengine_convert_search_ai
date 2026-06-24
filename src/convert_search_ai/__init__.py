"""convert_search_ai — FileEngine conversion, search, and RAG-chat microservice.

M0 (scaffolding): package layout, environment Config, the LDAP auth + gRPC core
client reused from the FileEngine ecosystem, a FastAPI app with health/readiness,
the Postgres baseline migration, and a pytest harness with ``@live`` gating.

Conversion/extraction (M1), full-text search (M2), and vector RAG chat (M3) are
built on top of this skeleton — see design_documents/DEVELOPMENT_PLAN.md."""

__version__ = "0.1.0"

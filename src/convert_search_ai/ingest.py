"""Ingest worker — turn file-activity events into conversions.

Maps the core publisher's events (EVENT_CONTRACT.md) onto pipeline actions:
- file.created / file.updated / file.restored → (re)convert the file
- file.deleted                                → drop our document row (core cascades renditions)
- is_rendition events                         → ignored (our own output; avoids a feedback loop)
- dir.* / acl.* / role.*                      → ignored here (governance feeds M2's permission cache)

Conversion is idempotent, so at-least-once redelivery is safe; the reconcile
sweep is the backstop for anything missed during an outage."""
from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger("convert_search_ai.ingest")

_CONVERT_TYPES = {"file.created", "file.updated", "file.restored"}


class Ingestor:
    def __init__(self, config: Config, pipeline, store, source):
        self.config = config
        self.pipeline = pipeline
        self.store = store
        self.source = source

    def handle(self, event: dict) -> None:
        if event.get("is_rendition"):
            return  # our own rendition write — never recurse on it
        etype = event.get("type", "")
        uid = event.get("file_uid", "")
        tenant = event.get("tenant") or "default"
        if not uid:
            return
        if etype in _CONVERT_TYPES:
            outcome = self.pipeline.convert(uid, tenant)
            log.info("convert %s -> %s (%s)", uid, outcome.status, outcome.detail)
        elif etype == "file.deleted":
            self.store.delete(tenant, uid)
            log.info("deleted document rows for %s", uid)
        # other types are intentionally not conversion concerns

    def run_once(self, count: int = 32, block_ms: int = 5000) -> int:
        """Read one batch, handle each, ack. Returns the number processed."""
        batch = self.source.read(count=count, block_ms=block_ms)
        for msg_id, event in batch:
            try:
                self.handle(event)
            except Exception:  # never let one bad event stall the loop
                log.exception("failed handling event %s", msg_id)
            finally:
                # Ack regardless: conversion is idempotent and reconcile backstops
                # genuine misses. (A DLQ/no-ack-on-failure path is M1+ hardening.)
                self.source.ack([msg_id])
        return len(batch)

    def run_forever(self) -> None:
        self.source.ensure_group()
        log.info("ingest worker started; stream=%s group=%s",
                 self.config.events_stream, self.config.events_group)
        while True:
            self.run_once()


def build_ingestor(config: Config) -> Ingestor:
    """Assemble the worker from config: agent gRPC client, store, pipeline, source."""
    from .core_client import agent_client
    from .events import RedisEventSource
    from .indexing import Indexer
    from .pipeline import ConversionPipeline
    from .store import DocumentStore

    mf = agent_client(config)
    store = DocumentStore(config)
    pipeline = ConversionPipeline(mf=mf, store=store, config=config, indexer=Indexer(config))
    source = RedisEventSource(config)
    return Ingestor(config, pipeline, store, source)


def main() -> None:
    from .config import load_dotenv

    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    build_ingestor(Config()).run_forever()


if __name__ == "__main__":
    main()

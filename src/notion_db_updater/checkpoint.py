"""Durable checkpointer wiring (ADR 0006 / 0007) — the substrate for HITL resume.

`AsyncSqliteSaver.from_conn_string()` constructs the saver with LangGraph's *default*
serializer, which (as of langgraph 1.2.6) only *warns* when it deserializes a project type
it hasn't been told about — "unregistered type … will be blocked in a future version". A
Phase-6b paused run stores the whole `EnrichmentState` — including our `Entry` / `Candidate`
/ `RTHit` / `EnrichedEntry` — to disk and restores it after a process restart, so those types
must be **explicitly allowlisted** or a future langgraph will refuse to load the checkpoint
and resume will break. `open_checkpointer` is the single place that opens a saver with that
allowlist applied, used by both the reconcile `Runtime` and the single-Entry `--enrich` /
`--resume` CLI so every path checkpoints identically.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .models import Entry
from .omdb import Candidate
from .schema import EnrichedEntry
from .search import RTHit

# Every custom type carried in `EnrichmentState` that the msgpack serde would otherwise flag as
# "unregistered" — the (module, class-name) tuples langgraph's allowlist wants (bare module
# strings silently *block* rather than allow). Derived from the live classes via
# `__module__`/`__qualname__` rather than hardcoded strings, so a type that *moves* modules
# (as `RTHit` did firecrawl → search in Phase 8 — a stale hardcoded tuple there silently
# blocked deserialization and crashed `resolve_rt` on resume) keeps its allowlist entry.
# Add a class here whenever a new non-stdlib type joins the graph state.
_ALLOWED_TYPES = (Entry, Candidate, RTHit, EnrichedEntry)
_ALLOWED_MSGPACK_MODULES = [(t.__module__, t.__qualname__) for t in _ALLOWED_TYPES]


def checkpoint_serde() -> JsonPlusSerializer:
    """The graph checkpointer's serializer, allowlisting our state types (ADR 0007)."""
    return JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_MSGPACK_MODULES)


@asynccontextmanager
async def open_checkpointer(db_path: str) -> AsyncIterator[AsyncSqliteSaver]:
    """Open an `AsyncSqliteSaver` on `db_path` with our serde applied and its tables created.

    Mirrors `AsyncSqliteSaver.from_conn_string` (owns the aiosqlite connection lifecycle) but
    injects `checkpoint_serde()` — which `from_conn_string` gives no way to pass — and runs the
    async `setup()`. The connection is closed when the context exits.
    """
    async with aiosqlite.connect(db_path) as conn:
        saver = AsyncSqliteSaver(conn, serde=checkpoint_serde())
        await saver.setup()
        yield saver

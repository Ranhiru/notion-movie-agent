"""The enrichment `StateGraph` — Phase 2: a single OMDb source, one Entry, end-to-end.

Built as a LangGraph `StateGraph` (not loose functions) from the start so later phases bolt
on without a port: the linear chain `read_page → omdb → update_notion` keeps the node names of
the eventual full graph, so Phase 4 fans OMDb ‖ RT out of `omdb`, Phase 5 inserts a Judge
before `update_notion`, and Phase 6 adds conditional edges + a checkpointer.

Phase 2 scope (ADR 0004): OMDb result counts **0 and 1** only.
  - 1 candidate  → enrich, `done`.
  - 0 candidates → `failed` (definitive not-found).
  - >1 candidate → left `pending` (additive TODO for the Phase 6a disambiguation path).
Transient errors are *not* caught here — they propagate so nothing is written and the row
keeps its `pending`/empty status for the next run (RetryPolicy is Phase 7).

Clients are injected via `build_graph(notion, omdb)` (bound into nodes with `partial`); the
caller owns their async-context lifecycles.
"""

from __future__ import annotations

import functools
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from .models import Entry, enrichment_properties
from .notion import NotionClient
from .omdb import Candidate, OMDbClient, details_fields


class EnrichmentState(TypedDict):
    """Graph state for one Entry's enrichment run. `page_id` is the only required input."""

    page_id: str
    entry: NotRequired[Entry | None]
    candidates: NotRequired[list[Candidate]]  # full OMDb list (Phase 6a consumes >1)
    imdb_id: NotRequired[str | None]
    imdb_rating: NotRequired[float | None]
    plot: NotRequired[str | None]
    genre: NotRequired[str | None]
    status: NotRequired[str]  # done | failed | pending
    note: NotRequired[str]  # why failed / deferred — surfaced in the LangSmith trace


async def read_page(state: EnrichmentState, *, notion: NotionClient) -> dict:
    """Fetch the Entry; a blank entry is a definitive skip (don't search OMDb for "")."""
    entry = await notion.get_entry(state["page_id"])
    if not entry.name:
        return {"entry": entry, "status": "failed", "note": "blank Entry — skipped"}
    return {"entry": entry}


async def omdb(state: EnrichmentState, *, omdb: OMDbClient) -> dict:
    """Resolve the Entry against OMDb. Handles the 0- and 1-candidate cases (Phase 2 scope)."""
    if state.get("status") == "failed":
        return {}  # read_page already resolved this (blank Entry)

    entry = state.get("entry")
    assert entry is not None and entry.name is not None  # guaranteed by read_page
    candidates = await omdb.search(entry.name, entry.media_type)

    if not candidates:
        return {"candidates": candidates, "status": "failed", "note": "omdb: not found"}
    if len(candidates) > 1:
        # TODO(phase 6a): surface the full candidate list → LLM pre-filter → Slack HITL picker.
        return {
            "candidates": candidates,
            "status": "pending",
            "note": f"multi-candidate ({len(candidates)}); deferred to phase 6a",
        }

    details = await omdb.details(candidates[0].imdb_id)
    return {"candidates": candidates, "status": "done", **details_fields(details)}


async def update_notion(state: EnrichmentState, *, notion: NotionClient) -> dict:
    """Write partial data + final status back to Notion (idempotent upsert by page_id)."""
    props = enrichment_properties(
        imdb_rating=state.get("imdb_rating"),
        plot=state.get("plot"),
        genre=state.get("genre"),
        status=state.get("status", "pending"),
    )
    entry = await notion.update_entry(state["page_id"], props)
    return {"entry": entry}


def build_graph(notion: NotionClient, omdb_client: OMDbClient):
    """Compile `read_page → omdb → update_notion` with the clients bound into the nodes."""
    g = StateGraph(EnrichmentState)
    g.add_node("read_page", functools.partial(read_page, notion=notion))
    g.add_node("omdb", functools.partial(omdb, omdb=omdb_client))
    g.add_node("update_notion", functools.partial(update_notion, notion=notion))

    g.add_edge(START, "read_page")
    g.add_edge("read_page", "omdb")
    g.add_edge("omdb", "update_notion")
    g.add_edge("update_notion", END)

    return g.compile()  # no checkpointer yet — that arrives with interrupt() in Phase 6a

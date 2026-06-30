"""The enrichment `StateGraph` — Phase 4: parallel OMDb ‖ RT lanes, fan-out / fan-in.

Built as a LangGraph `StateGraph` (not loose functions) from the start so later phases bolt
on without a port. Phase 4 splits the lane after `read_page`:

                    ┌──→ omdb ─────────────┐
    read_page ──────┤                      ├──→ assemble ──→ update_notion ──→ END
                    └──→ rt (subgraph) ─────┘

- **Fan-out** = two edges out of `read_page`; **fan-in** = two edges into `assemble`, which
  LangGraph holds until *both* lanes complete (a barrier — proven in spike 02).
- The two lanes write **disjoint** state channels (omdb → status + imdb/plot/genre; rt →
  rt_critic/rt_audience), so the concurrent fan-out needs no reducer.
- `assemble` is a thin deterministic barrier today; it is the slot where Phase 5's
  LLM-as-judge lands (ADR 0008) — a named node, not both lanes wired straight to update_notion.

OMDb resolution scope is unchanged from Phase 2 (counts 0 and 1; >1 deferred to Phase 6a).
Transient OMDb errors still propagate (nothing written → Entry stays pending; RetryPolicy is
Phase 7). The RT lane, by contrast, swallows its own errors (best-effort; ADR 0004) — see
`rt.py`.

Clients/model are injected via `build_graph(...)` (bound into nodes with `partial`); the caller
owns their async-context lifecycles.
"""

from __future__ import annotations

import functools
from typing import NotRequired, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .firecrawl import FirecrawlClient
from .models import Entry, enrichment_properties
from .notion import NotionClient
from .omdb import Candidate, OMDbClient, details_fields
from .rt import build_rt_subgraph


class EnrichmentState(TypedDict):
    """Graph state for one Entry's enrichment run. `page_id` is the only required input."""

    page_id: str
    entry: NotRequired[Entry | None]
    candidates: NotRequired[list[Candidate]]  # full OMDb list (Phase 6a consumes >1)
    imdb_id: NotRequired[str | None]
    imdb_rating: NotRequired[float | None]
    plot: NotRequired[str | None]
    genre: NotRequired[str | None]
    rt_critic: NotRequired[int | None]  # Tomatometer — written by the RT lane (best-effort)
    rt_audience: NotRequired[int | None]  # Popcornmeter — written by the RT lane (best-effort)
    status: NotRequired[str]  # done | failed | pending
    note: NotRequired[str]  # why failed / deferred — surfaced in the LangSmith trace


async def read_page(state: EnrichmentState, *, notion: NotionClient) -> dict:
    """Fetch the Entry; a blank entry is a definitive skip (don't search OMDb for "")."""
    entry = await notion.get_entry(state["page_id"])
    if not entry.title:
        return {"entry": entry, "status": "failed", "note": "blank Entry — skipped"}
    return {"entry": entry}


async def omdb(state: EnrichmentState, *, omdb: OMDbClient) -> dict:
    """Resolve the Entry against OMDb. Handles the 0- and 1-candidate cases (Phase 2 scope)."""
    if state.get("status") == "failed":
        return {}  # read_page already resolved this (blank Entry)

    entry = state.get("entry")
    assert entry is not None and entry.title is not None  # guaranteed by read_page
    candidates = await omdb.search(entry.title, entry.media_type)

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


async def assemble(state: EnrichmentState) -> dict:
    """Fan-in barrier: runs only after *both* lanes complete (two edges into this node).

    Deterministic and near-empty in Phase 4 — the lanes have already written their disjoint
    channels into state, so there is nothing to reconcile (ADR 0008: deterministic assembly,
    not a merge). It exists as the named join point where Phase 5's LLM-as-judge lands (it will
    inspect the assembled OMDb + RT record and emit `confidence`). Returns no updates today.
    """
    return {}


async def update_notion(state: EnrichmentState, *, notion: NotionClient) -> dict:
    """Write partial data + final status back to Notion (idempotent upsert by page_id).

    RT scores are written alongside IMDb/plot/genre but are null-safe: a soft-miss RT simply
    isn't written and never changes the OMDb-decided status (ADR 0004 — RT can't block `done`).
    """
    props = enrichment_properties(
        imdb_rating=state.get("imdb_rating"),
        plot=state.get("plot"),
        genre=state.get("genre"),
        rt_critic=state.get("rt_critic"),
        rt_audience=state.get("rt_audience"),
        status=state.get("status", "pending"),
    )
    entry = await notion.update_entry(state["page_id"], props)
    return {"entry": entry}


def build_graph(
    notion: NotionClient,
    omdb_client: OMDbClient,
    firecrawl: FirecrawlClient,
    extraction_llm: ChatOpenAI,
):
    """Compile the fan-out/fan-in enrichment graph with clients + model bound into the nodes.

    The RT lane is a compiled subgraph (`rt.py`) embedded as the single `rt` node, so it nests
    in LangSmith/Studio and grows into Phase 8's provider chain in place.
    """
    rt_subgraph = build_rt_subgraph(firecrawl, extraction_llm)

    g = StateGraph(EnrichmentState)
    g.add_node("read_page", functools.partial(read_page, notion=notion))
    g.add_node("omdb", functools.partial(omdb, omdb=omdb_client))
    g.add_node("rt", rt_subgraph)  # compiled subgraph as a node (shared keys: entry, rt_*)
    g.add_node("assemble", assemble)
    g.add_node("update_notion", functools.partial(update_notion, notion=notion))

    g.add_edge(START, "read_page")
    g.add_edge("read_page", "omdb")  # fan-out: OMDb lane
    g.add_edge("read_page", "rt")  # fan-out: RT lane (parallel)
    g.add_edge("omdb", "assemble")  # fan-in: barrier waits for both lanes
    g.add_edge("rt", "assemble")
    g.add_edge("assemble", "update_notion")
    g.add_edge("update_notion", END)

    return g.compile()  # no checkpointer yet — that arrives with interrupt() in Phase 6a

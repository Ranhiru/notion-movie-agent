"""The enrichment `StateGraph` — Phase 5: parallel lanes, deterministic assemble, Judge.

Built as a LangGraph `StateGraph` (not loose functions) from the start so later phases bolt
on without a port. The lane splits after `read_page` and rejoins at `assemble`:

                ┌──→ omdb ───────┐
    read_page ──┤                ├──→ assemble → resolve_rt → judge → update_notion → END
                └──→ rt ─────────┘

- **Fan-out** = two edges out of `read_page`; **fan-in** = two edges into `assemble`, which
  LangGraph holds until *both* lanes complete (a barrier — proven in spike 02).
- The two lanes write **disjoint** state channels (omdb → status + imdb/plot/genre + identity;
  rt → rt_* scores/identity/candidates), so the concurrent fan-out needs no reducer.
- `assemble` is the deterministic fan-in barrier (computes `sources_used`). `resolve_rt` and
  `judge` are linear post-fan-in nodes; both self-guard on `status`/candidate-count (no
  conditional edges yet — that is Phase 6a). `resolve_rt` correlates a >1 RT candidate set
  against OMDb's resolved identity; `judge` is the LLM-as-judge, building the `EnrichedEntry`
  output contract and emitting a trace-only `confidence` (ADR 0008).

OMDb resolution scope is unchanged from Phase 2 (counts 0 and 1; >1 deferred to Phase 6a).
Transient OMDb errors still propagate (nothing written → Entry stays pending; RetryPolicy is
Phase 7). The RT lane, `resolve_rt`, and `judge` all swallow their own errors (best-effort;
ADR 0004) — RT must never block `done`, and `confidence` is trace-only, so a judge failure
degrades to `confidence=low` rather than failing the Entry.

Clients/models are injected via `build_graph(...)` (bound into nodes with `partial`); the
caller owns their async-context lifecycles.
"""

from __future__ import annotations

import functools
import logging
from typing import NotRequired, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .firecrawl import FirecrawlClient, RTHit
from .models import Entry, enrichment_properties
from .notion import NotionClient
from .omdb import Candidate, OMDbClient, details_fields
from .rt import build_rt_subgraph, extract_rt_page, synopsis_region
from .schema import Confidence, EnrichedEntry, normalize_media_type

log = logging.getLogger(__name__)


class EnrichmentState(TypedDict):
    """Graph state for one Entry's enrichment run. `page_id` is the only required input."""

    page_id: str
    entry: NotRequired[Entry | None]
    candidates: NotRequired[list[Candidate]]  # full OMDb list (Phase 6a consumes >1)
    # OMDb lane — metadata + resolved identity (identity feeds the Judge, Phase 5 / ADR 0008)
    imdb_id: NotRequired[str | None]
    imdb_rating: NotRequired[float | None]
    plot: NotRequired[str | None]
    genre: NotRequired[str | None]
    omdb_title: NotRequired[str | None]
    year: NotRequired[int | None]
    omdb_type: NotRequired[str | None]  # OMDb "movie" | "series" | "episode"
    # RT lane — best-effort scores + matched-page identity + the (deferred) candidate set
    rt_candidates: NotRequired[list[RTHit]]  # >1 → correlated by resolve_rt; dropped after
    rt_url: NotRequired[str | None]
    rt_title: NotRequired[str | None]
    rt_year: NotRequired[int | None]
    rt_plot: NotRequired[str | None]  # RT synopsis — feeds resolve_rt correlation + the Judge
    rt_critic: NotRequired[int | None]  # Tomatometer — written by the RT lane (best-effort)
    rt_audience: NotRequired[int | None]  # Popcornmeter — written by the RT lane (best-effort)
    status: NotRequired[str]  # done | failed | pending
    note: NotRequired[str]  # why failed / deferred — surfaced in the LangSmith trace
    # Judge output (Phase 5) — all trace-only; NOT written to Notion (§8 unchanged)
    enriched: NotRequired[EnrichedEntry | None]  # the assembled output contract
    sources_used: NotRequired[list[str]]
    confidence: NotRequired[Confidence]
    wrong_match: NotRequired[bool]
    judge_reason: NotRequired[str]


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


class RTMatch(BaseModel):
    """`resolve_rt`'s structured output: which RT candidate matches the OMDb identity."""

    index: int | None = Field(
        None, description="0-based index of the matching RT candidate, or null if none match"
    )
    reason: str = Field("", description="brief justification for the pick (or the no-match)")


class JudgeVerdict(BaseModel):
    """The Judge's structured output — a wrong-match assessment + a confidence grade."""

    confidence: Confidence = Field(description="high | medium | low")
    wrong_match: bool = Field(
        description="true if the OMDb and RT lanes resolved different titles"
    )
    reason: str = Field("", description="brief justification for the confidence / flag")


async def assemble(state: EnrichmentState) -> dict:
    """Fan-in barrier: runs only after *both* lanes complete (two edges into this node).

    Deterministic — the lanes have already written their disjoint channels into state, so there
    is nothing to reconcile (ADR 0008: deterministic assembly, not a merge). It records which
    lanes contributed (`sources_used`) and is the named join before `resolve_rt` / `judge`. For
    a non-resolved Entry (blank / not-found / multi-candidate) there is nothing to assemble.
    """
    if state.get("status") != "done":
        return {}
    # Both lanes always run for a resolved Entry (the RT lane may soft-miss, but it did run).
    return {"sources_used": ["omdb", "firecrawl"]}


async def resolve_rt(state: EnrichmentState, *, llm: ChatOpenAI) -> dict:
    """Correlate a >1 RT candidate set against OMDb's resolved identity (ADR 0003 / 0008).

    Only fires on the ambiguous tail: a resolved Entry (`status == "done"`) with more than one
    canonical RT page in contention (the single-match / soft-miss cases are already settled by
    the RT subgraph). **Metadata-first** — the LLM picks the candidate matching OMDb's
    identity, reading OMDb's title/year **and plot** against each candidate's title/year **and
    synopsis** (title/year alone can't separate same-name titles — *Orphan Black* 2013 vs
    *Echoes* 2024). Candidate synopsis slices are cheap (already-scraped markdown, no LLM);
    scores + the full plot are then extracted **once** from the winner. Best-effort: on an LLM
    error it falls back to the deterministic top pick, so RT never blocks `done`. Drops
    `rt_candidates` afterwards (Phase 6 checkpoint hygiene).
    """
    candidates = state.get("rt_candidates") or []
    if state.get("status") != "done" or len(candidates) <= 1:
        return {}

    listing = "\n\n".join(
        f"[{i}] {c.title!r} (year: {c.year}) — {c.url}\n"
        f"    synopsis: {synopsis_region(c.markdown) or '(none found)'}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "OMDb resolved this title as:\n"
        f"  title: {state.get('omdb_title')!r}\n"
        f"  year: {state.get('year')}\n"
        f"  plot: {state.get('plot')!r}\n\n"
        "Pick the ONE Rotten Tomatoes page below that is the same title — matching on year "
        "and on whether the synopsis describes the same story as OMDb's plot. If none is a "
        "plausible match, return index=null.\n\n"
        f"{listing}"
    )
    try:
        match = await llm.with_structured_output(RTMatch).ainvoke(prompt)
        assert isinstance(match, RTMatch)
        idx = match.index
    except Exception:  # noqa: BLE001 — RT is best-effort; fall back to the deterministic pick
        log.exception("resolve_rt: correlation failed — falling back to top-ranked candidate")
        idx = 0

    if idx is None:
        # No candidate the Judge accepts → generalized soft miss (ADR 0003). Drop the set.
        return {"rt_candidates": []}
    if not 0 <= idx < len(candidates):
        idx = 0  # defend against an out-of-range index from the model

    winner = candidates[idx]
    page = await extract_rt_page(llm, winner.markdown)
    return {
        "rt_url": winner.url,
        "rt_title": winner.title,
        "rt_year": winner.year,
        "rt_plot": page.plot,
        "rt_critic": page.rt_critic,
        "rt_audience": page.rt_audience,
        "rt_candidates": [],  # drop candidate markdown from state (checkpoint hygiene)
    }


async def judge(state: EnrichmentState, *, llm: ChatOpenAI) -> dict:
    """LLM-as-judge fan-in guard (ADR 0008): the check against enriching the *wrong* title.

    Inspects the assembled record's **two independently-resolved identities** — OMDb's
    title/year/imdb_id/rating/plot/genre ‖ the winning RT page's title/url/year/plot/scores —
    for (a) a cross-lane title/year/plot mismatch and (b) score implausibility, then emits
    `confidence`.
    Builds the `EnrichedEntry` output contract. Runs only for a resolved Entry; best-effort —
    an LLM error degrades to `confidence=low` (surfaces the row for review) rather than failing
    it. `confidence` / `wrong_match` are trace-only — never written to Notion (§8 unchanged).
    """
    if state.get("status") != "done":
        return {}

    entry = state.get("entry")
    prompt = (
        "You are auditing a movie/TV enrichment for a wrong match. Two independent lanes "
        "resolved this with no shared key, so they may have landed on different titles.\n\n"
        "OMDb (metadata lane):\n"
        f"  title: {state.get('omdb_title')!r}\n  year: {state.get('year')}\n"
        f"  imdb_id: {state.get('imdb_id')}\n"
        f"  imdb_rating: {state.get('imdb_rating')} (0-10)\n"
        f"  genre: {state.get('genre')!r}\n  plot: {state.get('plot')!r}\n\n"
        "Rotten Tomatoes (RT lane):\n"
        f"  title: {state.get('rt_title')!r}\n  year: {state.get('rt_year')}\n"
        f"  url: {state.get('rt_url')}\n"
        f"  plot: {state.get('rt_plot')!r}\n"
        f"  critic: {state.get('rt_critic')} (0-100)\n"
        f"  audience: {state.get('rt_audience')} (0-100)\n\n"
        "Flag wrong_match=true if the RT page is a different title/year — or describes a "
        "different story (compare the two plots) — than OMDb's. Grade confidence "
        "high/medium/low, weighing title/year/plot agreement and score plausibility "
        "(e.g. IMDb 9/10 alongside RT 15% is implausible). Null RT scores are acceptable "
        "(best-effort) and are not, on their own, low confidence."
    )
    try:
        verdict = await llm.with_structured_output(JudgeVerdict).ainvoke(prompt)
        assert isinstance(verdict, JudgeVerdict)
        confidence = verdict.confidence
        wrong_match = verdict.wrong_match
        reason = verdict.reason
    except Exception:  # noqa: BLE001 — trace-only signal; never fail the Entry over the Judge
        log.exception("judge: verdict failed — degrading to confidence=low")
        confidence, wrong_match, reason = "low", False, "judge unavailable"

    enriched = EnrichedEntry(
        title=state.get("omdb_title") or (entry.title if entry else None) or "",
        year=state.get("year"),
        media_type=normalize_media_type(
            state.get("omdb_type") or (entry.media_type if entry else None)
        ),
        imdb_id=state.get("imdb_id"),
        imdb_rating=state.get("imdb_rating"),
        rt_critic=state.get("rt_critic"),
        rt_audience=state.get("rt_audience"),
        plot=state.get("plot"),
        genre=state.get("genre"),
        confidence=confidence,
        sources_used=state.get("sources_used", []),
    )
    return {
        "enriched": enriched,
        "confidence": confidence,
        "wrong_match": wrong_match,
        "judge_reason": reason,
    }


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
    judge_llm: ChatOpenAI,
):
    """Compile the fan-out/fan-in enrichment graph with clients + models bound into the nodes.

    The RT lane is a compiled subgraph (`rt.py`) embedded as the single `rt` node, so it nests
    in LangSmith/Studio and grows into Phase 8's provider chain in place. `judge_llm` drives
    both the post-fan-in `resolve_rt` correlation and the `judge` node (Phase 5).
    """
    rt_subgraph = build_rt_subgraph(firecrawl, extraction_llm)

    g = StateGraph(EnrichmentState)
    g.add_node("read_page", functools.partial(read_page, notion=notion))
    g.add_node("omdb", functools.partial(omdb, omdb=omdb_client))
    g.add_node("rt", rt_subgraph)  # compiled subgraph as a node (shared keys: entry, rt_*)
    g.add_node("assemble", assemble)
    g.add_node("resolve_rt", functools.partial(resolve_rt, llm=judge_llm))
    g.add_node("judge", functools.partial(judge, llm=judge_llm))
    g.add_node("update_notion", functools.partial(update_notion, notion=notion))

    g.add_edge(START, "read_page")
    g.add_edge("read_page", "omdb")  # fan-out: OMDb lane
    g.add_edge("read_page", "rt")  # fan-out: RT lane (parallel)
    g.add_edge("omdb", "assemble")  # fan-in: barrier waits for both lanes
    g.add_edge("rt", "assemble")
    g.add_edge("assemble", "resolve_rt")  # linear post-fan-in; nodes self-guard on status
    g.add_edge("resolve_rt", "judge")
    g.add_edge("judge", "update_notion")
    g.add_edge("update_notion", END)

    return g.compile()  # no checkpointer yet — that arrives with interrupt() in Phase 6a

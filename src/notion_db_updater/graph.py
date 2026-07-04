"""The enrichment `StateGraph` — Phase 6b: parallel lanes + HITL disambiguation (interrupt).

Built as a LangGraph `StateGraph` (not loose functions) from the start so later phases bolt
on without a port. The lane splits after `read_page` and rejoins at `assemble`:

                ┌─ omdb_search ─(≤1)───────────────────────────────┐
                │       │ (>1)                                      │
    read_page ──┤   disambiguate ─(confident)─────→ omdb_details ───┤
                │       │ (unsure / fail-safe)           ↑          │
                │   await_human ──(interrupt() ↔ resume)─┘          │
                └─ rt ─────────────────────────────────────────────┴─→ assemble
                                                    → resolve_rt → judge → update_notion → END

- **Fan-out** = two edges out of `read_page`; **fan-in** = two edges into `assemble`, which
  LangGraph holds until *both* lanes complete (a barrier — proven in spike 02). The OMDb lane
  is now variable-length (and can *pause*) but always terminates at `omdb_details`, so the
  fan-in stays a clean two-edge join (no conditional edge lands on `assemble`).
- The two lanes write **disjoint** state channels (omdb → status + imdb/plot/genre + identity;
  rt → rt_* scores/identity/candidates), so the concurrent fan-out needs no reducer.
- **OMDb disambiguation (ADR 0008 role 2):** `omdb_search` surfaces the full OMDb candidate
  list; `route_after_search` routes >1 candidates to the LLM `disambiguate` pre-filter, ≤1
  straight to `omdb_details`. `disambiguate` picks the best candidate and self-assesses
  `confident`.
- **Phase 6b — HITL escalation (ADR 0006):** `route_after_disambiguate` takes a *confident*
  pick straight to `omdb_details`; an unsure pick (or a fail-safe) goes to `await_human`, which
  calls `interrupt()` — snapshotting state to the checkpointer, returning control to the sweep
  (which moves on to the next Entry), and pausing under `thread_id = page_id` until an
  out-of-band `Command(resume=<imdbID>)` supplies the human's pick. `omdb_details` fetches
  details for the resolved `chosen_imdb_id` and is kept *downstream* of the interrupt: an
  interrupt re-runs its whole node on resume, so the external OMDb call must not sit in
  `await_human`.
- `assemble` is the deterministic fan-in barrier (computes `sources_used`). `resolve_rt` and
  `judge` are linear post-fan-in nodes that self-guard on `status`/candidate-count.
  `resolve_rt` correlates a >1 RT candidate set against OMDb's resolved identity; `judge` is
  the LLM-as-judge, building the `EnrichedEntry` output contract and emitting a trace-only
  `confidence` (ADR 0008).

Transient OMDb errors still propagate (nothing written → Entry stays pending; RetryPolicy is
Phase 7). The RT lane, `resolve_rt`, and `judge` all swallow their own errors (best-effort;
ADR 0004) — RT must never block `done`, and `confidence` is trace-only, so a judge failure
degrades to `confidence=low` rather than failing the Entry. `disambiguate`, by contrast, is a
correctness decision: it fails *safe* (defers the pick) rather than guessing an identity.

The graph is compiled with an `AsyncSqliteSaver` checkpointer (ADR 0006 / 0007), keyed by
`thread_id = page_id`, so a Phase-6b paused HITL run survives a process restart. Clients/models
are injected via `build_graph(...)` (bound into nodes with `partial`); the caller owns their
async-context lifecycles (incl. the checkpointer).
"""

from __future__ import annotations

import functools
import logging
from typing import NotRequired, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
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
    candidates: NotRequired[list[Candidate]]  # full OMDb list (>1 → disambiguate, Phase 6a)
    # OMDb disambiguation (Phase 6a / ADR 0008 role 2)
    chosen_imdb_id: NotRequired[
        str | None
    ]  # resolved candidate: single pick / pre-filter / human
    best_guess_imdb_id: NotRequired[
        str | None
    ]  # pre-filter's pick — stashed for the 6d timeout
    confident: NotRequired[bool]  # pre-filter self-assessment (drives 6b's escalate branch)
    disambiguation_reason: NotRequired[str]  # trace-only justification for the pick
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


async def omdb_search(state: EnrichmentState, *, omdb: OMDbClient) -> dict:
    """Search OMDb for the Entry's candidates — the head of the OMDb fan-out lane.

    Resolves the trivial cases inline: 0 → `failed` (definitive not-found, ADR 0004); exactly
    1 → the trivial pick, stashed in `chosen_imdb_id` (no LLM needed). A >1 set is carried for
    the `disambiguate` pre-filter (`route_after_search` decides). Details are fetched
    *separately* in `omdb_details`, after the pick is settled — so the OMDb `details` call
    never sits in a node that a Phase-6b `interrupt()` would re-run on resume.
    """
    if state.get("status") == "failed":
        return {}  # read_page already resolved this (blank Entry) → omdb_details will no-op

    entry = state.get("entry")
    assert entry is not None and entry.title is not None  # guaranteed by read_page
    candidates = await omdb.search(entry.title, entry.media_type)

    if not candidates:
        return {"candidates": candidates, "status": "failed", "note": "omdb: not found"}
    if len(candidates) == 1:
        return {"candidates": candidates, "chosen_imdb_id": candidates[0].imdb_id}
    return {"candidates": candidates}  # >1 → route_after_search sends this to disambiguate


def route_after_search(state: EnrichmentState) -> str:
    """Conditional route out of `omdb_search` (ADR 0008 role 2 — the first conditional edge).

    >1 candidates → the LLM `disambiguate` pre-filter. The 0-candidate (not-found / blank) and
    1-candidate cases both go straight to `omdb_details`, which fetches details for
    `chosen_imdb_id` or no-ops when there is none — so the OMDb lane always terminates at
    `omdb_details`, keeping the fan-in a clean two-edge barrier.
    """
    candidates = state.get("candidates") or []
    return "disambiguate" if len(candidates) > 1 else "omdb_details"


class DisambiguationPick(BaseModel):
    """The disambiguation pre-filter's structured output (ADR 0008 role 2)."""

    index: int | None = Field(
        None, description="0-based index of the best-matching candidate, or null if none fit"
    )
    confident: bool = Field(
        description="true only if a single candidate is a clear, unambiguous match"
    )
    reason: str = Field("", description="brief justification for the pick")


async def disambiguate(state: EnrichmentState, *, llm: ChatOpenAI) -> dict:
    """LLM disambiguation pre-filter (ADR 0008 role 2): pick the best OMDb candidate.

    Fires only on the ambiguous tail (>1 candidates). Returns a structured pick (a list index,
    validated back to a real candidate) plus a self-assessment (`confident`) that Phase 6b's
    conditional edge uses to decide whether to auto-take the pick or escalate to a human via
    `interrupt()`. In 6a the route *always* takes the pick.

    Unlike the best-effort RT / Judge nodes, this is a **correctness** decision — picking the
    wrong IMDb identity is the one thing this phase exists to prevent — so it fails *safe*: a
    transport error or an unusable index yields `confident=False` and *no* `chosen_imdb_id`
    (deferring the pick) rather than guessing. The fallback for uncertainty is a human (6b),
    not a wrong write. The pick is also stashed as `best_guess_imdb_id` for the 6d timeout.
    """
    candidates = state.get("candidates") or []
    entry = state.get("entry")
    listing = "\n".join(
        f"[{i}] {c.title!r} ({c.year}) — {c.media_type} — {c.imdb_id}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"A user added this to their watchlist: {(entry.title if entry else None)!r}"
        + (f" (type: {entry.media_type})" if entry and entry.media_type else "")
        + ".\n\nOMDb returned these candidates:\n\n"
        + listing
        + "\n\nPick the ONE candidate that best matches what the user meant. Set "
        "confident=true only if a single candidate is a clear, unambiguous match; if several "
        "are plausible (e.g. a remake and its original, or the same title across years), set "
        "confident=false so a human can decide."
    )
    try:
        pick = await llm.with_structured_output(DisambiguationPick).ainvoke(prompt)
        assert isinstance(pick, DisambiguationPick)  # narrow for the type checker
    except Exception:  # noqa: BLE001 — fail safe: defer the pick to a human, never guess
        log.exception("disambiguate: pre-filter failed — deferring the pick")
        return {"confident": False, "disambiguation_reason": "pre-filter unavailable"}

    idx = pick.index
    if idx is None or not 0 <= idx < len(candidates):
        # No usable pick → don't guess an identity; 6b escalates this to a human.
        return {"confident": False, "disambiguation_reason": pick.reason or "no valid pick"}
    chosen = candidates[idx].imdb_id
    return {
        "chosen_imdb_id": chosen,
        "best_guess_imdb_id": chosen,
        "confident": pick.confident,
        "disambiguation_reason": pick.reason,
    }


def route_after_disambiguate(state: EnrichmentState) -> str:
    """Conditional route out of `disambiguate` (Phase 6b — the HITL escalation fork).

    A **confident** pick goes straight to `omdb_details` (auto-resolved, no human). Anything
    else — a genuinely ambiguous set or a fail-safe (LLM error / unusable index →
    `confident=False`) — escalates to `await_human`, which pauses the run via `interrupt()`.
    Routes on `confident` alone, not on `chosen_imdb_id`: an unsure pick still stashes its best
    guess in `chosen_imdb_id` / `best_guess_imdb_id` (for the 6d timeout), but we still want a
    human to confirm it, so its presence must not short-circuit the escalation.
    """
    return "omdb_details" if state.get("confident") else "await_human"


async def await_human(state: EnrichmentState) -> dict:
    """Pause for a human to pick the right OMDb candidate (Phase 6b — the HITL interrupt).

    Reached only when `disambiguate` was not confident. `interrupt()` snapshots the graph state
    to the checkpointer and makes `graph.ainvoke()` *return* (so the reconcile sweep moves on
    to the next Entry, ADR 0006); the run stays paused under `thread_id = page_id` until it is
    resumed out-of-band with the human's chosen imdbID (`Command(resume=<imdbID>)`), which is
    what `interrupt()` then returns.

    Deliberately the *only* node on the escalation branch, and it does no external work:
    `interrupt()` re-runs its whole node from the top on resume, so the expensive OMDb
    `details` fetch is kept downstream in `omdb_details` (the same reason the 6a search/details
    split exists). The payload carries everything the Phase-6c Slack picker needs — the
    candidate list + the pre-filter's best guess — so resume needs no recomputation.
    """
    chosen_imdb_id = interrupt(_picker_payload(state))
    return {"chosen_imdb_id": chosen_imdb_id}


def _picker_payload(state: EnrichmentState) -> dict:
    """The interrupt payload — the HITL picker's data (surfaced to the Phase-6c Slack prompt).

    Plain JSON-friendly shapes (not the `Candidate` objects) so the transport layer can render
    it directly. `best_guess_imdb_id` is the pre-filter's stashed pick, used both to pre-select
    the Slack default and by the 6d stale-interrupt auto-resolve.
    """
    entry = state.get("entry")
    candidates = state.get("candidates") or []
    return {
        "reason": "disambiguation",
        "page_id": state["page_id"],
        "title": entry.title if entry else None,
        "media_type": entry.media_type if entry else None,
        "best_guess_imdb_id": state.get("best_guess_imdb_id"),
        "candidates": [
            {
                "index": i,
                "imdb_id": c.imdb_id,
                "title": c.title,
                "year": c.year,
                "media_type": c.media_type,
                "poster": c.poster,
            }
            for i, c in enumerate(candidates)
        ],
    }


async def omdb_details(state: EnrichmentState, *, omdb: OMDbClient) -> dict:
    """Fetch OMDb details for the resolved candidate → the metadata lane's output + identity.

    The single tail of the OMDb lane: reached for the 1-candidate case, the pre-filter's pick,
    and (Phase 6b) the human's pick alike. No-ops when there is no `chosen_imdb_id` — the
    not-found (0) and blank-Entry paths route here too so the lane has one exit into the
    fan-in, but there is nothing to fetch and the `failed` status stands.
    """
    chosen = state.get("chosen_imdb_id")
    if not chosen:
        return {}
    details = await omdb.details(chosen)
    return {"status": "done", **details_fields(details)}


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
    disambiguation_llm: ChatOpenAI,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Compile the fan-out/fan-in enrichment graph with clients + models bound into the nodes.

    The RT lane is a compiled subgraph (`rt.py`) embedded as the single `rt` node, so it nests
    in LangSmith/Studio and grows into Phase 8's provider chain in place. `judge_llm` drives
    both the post-fan-in `resolve_rt` correlation and the `judge` node (Phase 5);
    `disambiguation_llm` drives the Phase-6a `disambiguate` pre-filter (ADR 0008 role 2).

    `checkpointer` is an `AsyncSqliteSaver` (ADR 0006 / 0007) when durable execution is wanted
    (the reconcile Runtime, and single-Entry `--enrich`/`--resume`); pass `None` for pure
    topology work (e.g. drawing the graph), where no state is persisted.
    """
    rt_subgraph = build_rt_subgraph(firecrawl, extraction_llm)

    g = StateGraph(EnrichmentState)
    g.add_node("read_page", functools.partial(read_page, notion=notion))
    g.add_node("omdb_search", functools.partial(omdb_search, omdb=omdb_client))
    g.add_node("disambiguate", functools.partial(disambiguate, llm=disambiguation_llm))
    g.add_node("await_human", await_human)  # Phase 6b: interrupt() — no deps to bind
    g.add_node("omdb_details", functools.partial(omdb_details, omdb=omdb_client))
    g.add_node("rt", rt_subgraph)  # compiled subgraph as a node (shared keys: entry, rt_*)
    g.add_node("assemble", assemble)
    g.add_node("resolve_rt", functools.partial(resolve_rt, llm=judge_llm))
    g.add_node("judge", functools.partial(judge, llm=judge_llm))
    g.add_node("update_notion", functools.partial(update_notion, notion=notion))

    g.add_edge(START, "read_page")
    g.add_edge("read_page", "omdb_search")  # fan-out: OMDb lane
    g.add_edge("read_page", "rt")  # fan-out: RT lane (parallel)
    # >1 candidates → the LLM pre-filter; ≤1 → straight to details (ADR 0008 role 2).
    g.add_conditional_edges(
        "omdb_search", route_after_search, ["disambiguate", "omdb_details"]
    )
    # 6b: confident pick → details; unsure / fail-safe → pause for a human via interrupt().
    g.add_conditional_edges(
        "disambiguate", route_after_disambiguate, ["omdb_details", "await_human"]
    )
    g.add_edge("await_human", "omdb_details")  # resume lands here with the human's pick
    g.add_edge("omdb_details", "assemble")  # fan-in: barrier waits for both lanes
    g.add_edge("rt", "assemble")
    g.add_edge("assemble", "resolve_rt")  # linear post-fan-in; nodes self-guard on status
    g.add_edge("resolve_rt", "judge")
    g.add_edge("judge", "update_notion")
    g.add_edge("update_notion", END)

    return g.compile(checkpointer=checkpointer)

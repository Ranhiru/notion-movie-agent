"""The RT resolution subgraph — Phase 4 (ADR 0003 / 0008).

The Rotten Tomatoes lane, modelled as its own compiled `StateGraph` and embedded as a single
node (`rt`) in the parent enrichment graph. Embedding the *compiled subgraph* (rather than a
flat function) is deliberate: it nests in LangSmith/Studio so the lane's internals are visible.

Two nodes:
  rt_search → extract
`rt_search` takes any injected `SearchClient` (ADR 0003, amended): the Phase 8 provider chain
(Firecrawl ‖ Tavily ‖ Exa, round-robin) is a `RoundRobinSearchClient` composite passed here,
so the chain grows *behind* this node — no sibling provider nodes, no topology change. The
search client discovers + scrapes the RT pages, surfacing ranked canonical candidates (each
with inline markdown + identity). `extract` applies **hybrid escalation** (Phase 5): the common
single-match case is extracted inline (`with_structured_output` → `{plot, rt_critic,
rt_audience}`); a >1 ambiguous set is deferred to the parent `resolve_rt` node, which
correlates it against OMDb's identity (title/year **+ plot vs each candidate's synopsis**)
before extracting once from the winner.

**RT is best-effort and must never block `done` (ADR 0004).** This lane therefore *swallows*
its own failures: a soft miss (no RT page) or a hard failure (Firecrawl 5xx, timeout) both
resolve to null scores, never a raised exception. Contrast the OMDb lane, where a transient
error propagates and leaves the Entry `pending` for the next cron pass. Phase 7 (ADR 0013)
softens the transient case: `FirecrawlClient` now retries transient blips (429 / 5xx /
timeout) *internally*, below this swallow — so a momentary Firecrawl hiccup no longer
permanently nulls RT on an otherwise-`done` row. A retry that still exhausts (or fallback
providers, Phase 8) is the remaining backstop; a node-level `RetryPolicy` is deliberately
*not* used here, since its exhaustion would re-raise and break "RT never blocks `done`".
"""

from __future__ import annotations

import functools
import logging
from typing import NotRequired, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .models import Entry
from .search import RTHit, SearchClient

log = logging.getLogger(__name__)

# Spike 05 learning: RT scores can sit ~15k chars into the markdown (Dune ~10.4k, Last of Us
# ~14.8k); a blind [:8000] truncation dropped them. We locate the score region instead, then
# take a generous window around it — and cap total length to stay inside the local model's
# context. The first matching marker anchors the window.
_SCORE_MARKERS = ("Tomatometer", "Popcornmeter", "Audience Score", "Critics Score")
_WINDOW = 12000  # chars taken from the anchor; ~score block + surrounding context
_MAX_CHARS = 20000  # hard cap handed to the LLM (proven sufficient in spike 05)

# The synopsis lives in a separate, far-apart region from the scores (in the Dune fixture the
# scores sit ~10k in but the "Movie Info / Synopsis" block is ~34k in), so it needs its own
# anchor + a small window — a plot paragraph, not the whole section.
_SYNOPSIS_MARKERS = ("Synopsis", "Movie Info", "Series Info", "Show Info")
_SYNOPSIS_WINDOW = 1500  # chars from the anchor; enough for the synopsis paragraph


class RTPage(BaseModel):
    """Structured-output target for the extraction node: the two RT meters + the synopsis.

    The plot (synopsis) is pulled alongside the scores so the parent `resolve_rt` node can
    correlate an RT page against OMDb's plot — title/year alone is ambiguous for same-name
    titles (Phase 5). Every field is nullable (best-effort; ADR 0004).
    """

    plot: str | None = Field(None, description="the film/show synopsis (plot summary)")
    rt_critic: int | None = Field(None, ge=0, le=100, description="Tomatometer (critic) 0-100")
    rt_audience: int | None = Field(
        None, ge=0, le=100, description="Popcornmeter (audience) 0-100"
    )


class RTState(TypedDict):
    """RT subgraph state. `entry` is the shared input; the `rt_*` keys are shared outputs that
    merge back into the parent `EnrichmentState`.

    `rt_candidates` holds the ranked canonical RT pages (with inline markdown). On the single-
    match fast path `extract` resolves the scores + identity here; on the >1 ambiguous tail it
    leaves the candidates for the parent `resolve_rt` node to correlate against OMDb (Phase 5).
    """

    entry: Entry | None
    rt_candidates: NotRequired[list[RTHit]]
    rt_url: NotRequired[str | None]
    rt_title: NotRequired[str | None]
    rt_year: NotRequired[int | None]
    rt_plot: NotRequired[str | None]  # RT synopsis (feeds the resolve_rt correlation)
    rt_critic: NotRequired[int | None]
    rt_audience: NotRequired[int | None]


def _region(markdown: str, markers: tuple[str, ...], window: int) -> str:
    """Slice `markdown` to a `window`-char region anchored on the earliest of `markers`.

    Falls back to the document head if no marker is found. Always bounded by `_MAX_CHARS`.
    """
    anchor = min(
        (idx for m in markers if (idx := markdown.find(m)) != -1),
        default=0,
    )
    # Start a little before the anchor so the marker's own context isn't clipped.
    start = max(0, anchor - 500)
    return markdown[start : start + window][:_MAX_CHARS]


def _score_region(markdown: str) -> str:
    """Slice to the region likely holding the RT scores (spike 05 learning)."""
    return _region(markdown, _SCORE_MARKERS, _WINDOW)


def synopsis_region(markdown: str | None) -> str:
    """Slice to the region likely holding the RT synopsis (a small window around "Movie Info").

    Public because the parent graph's `resolve_rt` feeds these slices — one per candidate —
    into the correlation prompt alongside OMDb's plot, so the LLM can match same-name titles by
    story, not just title/year. Returns "" when there's no markdown / no synopsis marker.
    """
    if not markdown:
        return ""
    return _region(markdown, _SYNOPSIS_MARKERS, _SYNOPSIS_WINDOW)


async def extract_rt_page(llm: ChatOpenAI, markdown: str | None) -> RTPage:
    """Parse RT page markdown → `RTPage` (scores + synopsis) via `with_structured_output`.

    Shared by the subgraph's `extract` (fast path) and the parent graph's `resolve_rt` (the
    correlated winner), so the extraction prompt + slicing live in one place. The scores and
    the synopsis sit in far-apart regions of the page, so both slices are concatenated into one
    call. No markdown → empty result without an LLM call; a parse/transport error degrades to
    empty rather than raising (ADR 0004 — RT must never block `done`).
    """
    if not markdown:
        return RTPage(plot=None, rt_critic=None, rt_audience=None)
    context = _score_region(markdown)
    synopsis = synopsis_region(markdown)
    if synopsis:
        context = f"{context}\n\n--- MOVIE/SERIES INFO ---\n\n{synopsis}"
    prompt = (
        "From this Rotten Tomatoes page markdown, extract: the critic score (Tomatometer) and "
        "audience score (Popcornmeter) as integers 0-100, and the plot/synopsis as a short "
        "summary string. Use null for anything absent.\n\n" + context
    )
    try:
        page = await llm.with_structured_output(RTPage).ainvoke(prompt)
        assert isinstance(page, RTPage)  # narrow for the type checker
    except Exception:  # noqa: BLE001 — extraction failure is still just a missing RT field
        log.exception("rt: extraction failed — degrading to null RT")
        return RTPage(plot=None, rt_critic=None, rt_audience=None)
    return page


async def rt_search(state: RTState, *, search: SearchClient) -> dict:
    """Discover + scrape the Entry's RT pages → ranked candidates. Best-effort: never raises.

    Provider-agnostic (ADR 0003, amended): `search` is any `SearchClient` — a single provider
    (Firecrawl / Tavily / Exa) or the round-robin composite — so the rotation chain grows
    without changing this node. Skips a blank Entry (`title is None`) so we don't search "".
    A soft miss (no RT page) or any provider error both resolve to an empty candidate list, so
    the lane degrades to null scores rather than failing the Entry (ADR 0004).
    """
    entry = state.get("entry")
    if entry is None or entry.title is None:
        return {"rt_candidates": []}
    try:
        candidates = await search.search_rt_candidates(entry.title, entry.media_type)
    except Exception:  # noqa: BLE001 — RT must never block `done`; degrade to null scores
        log.exception("rt: search failed for %r — degrading to null RT", entry.title)
        candidates = []
    return {"rt_candidates": candidates}


async def extract(state: RTState, *, llm: ChatOpenAI) -> dict:
    """Hybrid escalation over the RT candidates (ADR 0003 / 0008):

    - **0 candidates** (soft miss) → null scores, no LLM call.
    - **exactly 1** → the deterministic fast path: extract scores inline and surface the page
      identity. ≥1 non-null score = a provider "success" (the signal Phase 8's chain uses).
    - **>1 in contention** → *defer*. Leave the candidate set in state (markdown already paid
      for) for the parent `resolve_rt` node to correlate against OMDb's resolved identity, then
      extract once from the winner. Extraction here would waste calls on pages we may discard.
    """
    candidates = state.get("rt_candidates") or []
    if not candidates:
        return {"rt_plot": None, "rt_critic": None, "rt_audience": None}
    if len(candidates) > 1:
        return {}  # defer to resolve_rt; candidates stay in state
    hit = candidates[0]
    page = await extract_rt_page(llm, hit.markdown)
    return {
        "rt_url": hit.url,
        "rt_title": hit.title,
        "rt_year": hit.year,
        "rt_plot": page.plot,
        "rt_critic": page.rt_critic,
        "rt_audience": page.rt_audience,
    }


def build_rt_subgraph(search: SearchClient, llm: ChatOpenAI):
    """Compile `rt_search → extract` with the search client + model bound into the nodes.

    `search` is any `SearchClient` (ADR 0003, amended): a single provider or the round-robin
    composite — the subgraph is provider-agnostic. Returned compiled graph is embedded as the
    `rt` node of the parent graph (see `graph.py`).
    """
    g = StateGraph(RTState)
    g.add_node("rt_search", functools.partial(rt_search, search=search))
    g.add_node("extract", functools.partial(extract, llm=llm))
    g.add_edge(START, "rt_search")
    g.add_edge("rt_search", "extract")
    g.add_edge("extract", END)
    return g.compile()

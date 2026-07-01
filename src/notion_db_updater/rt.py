"""The RT resolution subgraph — Phase 4 (ADR 0003 / 0008).

The Rotten Tomatoes lane, modelled as its own compiled `StateGraph` and embedded as a single
node (`rt`) in the parent enrichment graph. Embedding the *compiled subgraph* (rather than a
flat function) is deliberate: it nests in LangSmith/Studio so the lane's internals are visible,
and it's the structure Phase 8 grows into the full provider chain (Firecrawl → Tavily → Exa →
Perplexity) by adding sibling provider nodes + conditional edges.

**Firecrawl only** (fallback providers are Phase 8), two nodes:
  firecrawl_provider → extract
The provider discovers + scrapes the RT pages, surfacing ranked canonical candidates (each with
inline markdown + identity). `extract` applies **hybrid escalation** (Phase 5): the common
single-match case is extracted inline (`with_structured_output` → `{rt_critic, rt_audience}`);
a >1 ambiguous set is deferred to the parent `resolve_rt` node, which correlates it against
OMDb's resolved identity before extracting once from the winner.

**RT is best-effort and must never block `done` (ADR 0004).** This lane therefore *swallows*
its own failures: a soft miss (no RT page) or a hard failure (Firecrawl 5xx, timeout) both
resolve to null scores, never a raised exception. Contrast the OMDb lane, where a transient
error propagates and leaves the Entry `pending` for the next cron pass. Known Phase-4 cost:
with no `RetryPolicy` (Phase 7) or fallback providers (Phase 8) yet, a *transient* Firecrawl
error becomes a permanently-null RT on an otherwise-`done` row until a manual re-run.
"""

from __future__ import annotations

import functools
import logging
from typing import NotRequired, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .firecrawl import FirecrawlClient, RTHit
from .models import Entry

log = logging.getLogger(__name__)

# Spike 05 learning: RT scores can sit ~15k chars into the markdown (Dune ~10.4k, Last of Us
# ~14.8k); a blind [:8000] truncation dropped them. We locate the score region instead, then
# take a generous window around it — and cap total length to stay inside the local model's
# context. The first matching marker anchors the window.
_SCORE_MARKERS = ("Tomatometer", "Popcornmeter", "Audience Score", "Critics Score")
_WINDOW = 12000  # chars taken from the anchor; ~score block + surrounding context
_MAX_CHARS = 20000  # hard cap handed to the LLM (proven sufficient in spike 05)


class RTScores(BaseModel):
    """Structured-output target for the extraction node — the two RT meters, each nullable."""

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
    rt_critic: NotRequired[int | None]
    rt_audience: NotRequired[int | None]


def _score_region(markdown: str) -> str:
    """Slice the markdown to the region likely holding the scores (spike 05 learning).

    Anchors on the earliest score marker and takes a window from there; falls back to the
    head of the document if no marker is found. Always bounded by `_MAX_CHARS`.
    """
    anchor = min(
        (idx for m in _SCORE_MARKERS if (idx := markdown.find(m)) != -1),
        default=0,
    )
    # Start a little before the anchor so the marker's own context isn't clipped.
    start = max(0, anchor - 500)
    return markdown[start : start + _WINDOW][:_MAX_CHARS]


async def extract_rt_scores(llm: ChatOpenAI, markdown: str | None) -> RTScores:
    """Parse RT page markdown → `RTScores` via `with_structured_output`. Best-effort.

    Shared by the subgraph's `extract` (fast path) and the parent graph's `resolve_rt` (the
    correlated winner), so the extraction prompt + slicing live in one place. No markdown →
    empty scores without an LLM call; a parse/transport error degrades to empty rather than
    raising (ADR 0004 — RT must never block `done`).
    """
    if not markdown:
        return RTScores(rt_critic=None, rt_audience=None)
    prompt = (
        "From this Rotten Tomatoes page markdown, extract the critic score (Tomatometer) and "
        "audience score (Popcornmeter) as integers 0-100. Use null if a score is absent.\n\n"
        + _score_region(markdown)
    )
    try:
        scores = await llm.with_structured_output(RTScores).ainvoke(prompt)
        assert isinstance(scores, RTScores)  # narrow for the type checker
    except Exception:  # noqa: BLE001 — extraction failure is still just a missing RT score
        log.exception("rt: extraction failed — degrading to null RT")
        return RTScores(rt_critic=None, rt_audience=None)
    return scores


async def firecrawl_provider(state: RTState, *, firecrawl: FirecrawlClient) -> dict:
    """Discover + scrape the Entry's RT pages → ranked candidates. Best-effort: never raises.

    Skips a blank Entry (`title is None`) so we don't search for "". A soft miss (no RT page)
    or any Firecrawl error both resolve to an empty candidate list, so the lane degrades to
    null scores rather than failing the Entry (ADR 0004).
    """
    entry = state.get("entry")
    if entry is None or entry.title is None:
        return {"rt_candidates": []}
    try:
        candidates = await firecrawl.search_rt_candidates(entry.title, entry.media_type)
    except Exception:  # noqa: BLE001 — RT must never block `done`; degrade to null scores
        log.exception("rt: firecrawl failed for %r — degrading to null RT", entry.title)
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
        return {"rt_critic": None, "rt_audience": None}
    if len(candidates) > 1:
        return {}  # defer to resolve_rt; candidates stay in state
    hit = candidates[0]
    scores = await extract_rt_scores(llm, hit.markdown)
    return {
        "rt_url": hit.url,
        "rt_title": hit.title,
        "rt_year": hit.year,
        "rt_critic": scores.rt_critic,
        "rt_audience": scores.rt_audience,
    }


def build_rt_subgraph(firecrawl: FirecrawlClient, llm: ChatOpenAI):
    """Compile `firecrawl_provider → extract` with the client + model bound into the nodes.

    Returned compiled graph is embedded as the `rt` node of the parent graph (see `graph.py`).
    """
    g = StateGraph(RTState)
    g.add_node(
        "firecrawl_provider", functools.partial(firecrawl_provider, firecrawl=firecrawl)
    )
    g.add_node("extract", functools.partial(extract, llm=llm))
    g.add_edge(START, "firecrawl_provider")
    g.add_edge("firecrawl_provider", "extract")
    g.add_edge("extract", END)
    return g.compile()

"""The RT resolution subgraph — Phase 4 (ADR 0003 / 0008).

The Rotten Tomatoes lane, modelled as its own compiled `StateGraph` and embedded as a single
node (`rt`) in the parent enrichment graph. Embedding the *compiled subgraph* (rather than a
flat function) is deliberate: it nests in LangSmith/Studio so the lane's internals are visible,
and it's the structure Phase 8 grows into the full provider chain (Firecrawl → Tavily → Exa →
Perplexity) by adding sibling provider nodes + conditional edges.

Phase 4 = **Firecrawl only**, two nodes:
  firecrawl_provider → extract
The provider discovers + scrapes the RT page (markdown); `extract` is the project's first
`with_structured_output` node, parsing the markdown into `{rt_critic, rt_audience}`.

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

from .firecrawl import FirecrawlClient
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
    """RT subgraph state. `entry` is the shared input; `rt_critic`/`rt_audience` the shared
    outputs that merge back into the parent `EnrichmentState`. `rt_markdown` is subgraph-only
    scratch (it has no parent channel, so it does not propagate up)."""

    entry: Entry | None
    rt_markdown: NotRequired[str | None]
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


async def firecrawl_provider(state: RTState, *, firecrawl: FirecrawlClient) -> dict:
    """Discover + scrape the Entry's RT page → markdown. Best-effort: never raises.

    Skips a blank Entry (`title is None`) so we don't search for "". A soft miss (no RT page)
    or any Firecrawl error both resolve to `rt_markdown=None`, so the lane degrades to null
    scores rather than failing the Entry (ADR 0004).
    """
    entry = state.get("entry")
    if entry is None or entry.title is None:
        return {"rt_markdown": None}
    try:
        markdown = await firecrawl.search_rt(entry.title, entry.media_type)
    except Exception:  # noqa: BLE001 — RT must never block `done`; degrade to null scores
        log.exception("rt: firecrawl failed for %r — degrading to null RT", entry.title)
        markdown = None
    return {"rt_markdown": markdown}


async def extract(state: RTState, *, llm: ChatOpenAI) -> dict:
    """First `with_structured_output` node: parse RT markdown → {rt_critic, rt_audience}.

    No markdown (soft miss / skipped) → null scores without an LLM call. The structured-output
    parse is best-effort too: a parse/transport error degrades to null rather than raising
    (ADR 0004). ≥1 non-null score = a provider "success" — the signal Phase 8's chain uses to
    decide whether to fall through to the next provider.
    """
    markdown = state.get("rt_markdown")
    if not markdown:
        return {"rt_critic": None, "rt_audience": None}
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
        return {"rt_critic": None, "rt_audience": None}
    return {"rt_critic": scores.rt_critic, "rt_audience": scores.rt_audience}


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

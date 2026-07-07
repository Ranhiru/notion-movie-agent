"""Async Tavily client — an RT search provider peer of Firecrawl (Phase 8, ADR 0003 amended).

A full `SearchClient` (search + inline page content + the same candidate shaping): Tavily's
`/search` scopes to rottentomatoes.com via `include_domains` and returns each hit's full page
text via `include_raw_content`, which feeds the same `rank_rt_hits` → `RTHit` pipeline every
provider shares. Parity with Firecrawl is what makes the round-robin rotation safe — a rotated
provider must deliver markdown-bearing candidates the downstream `extract` / `resolve_rt` nodes
can score, not a thin "try this URL" fallback.

Thin httpx wrapper, mirroring `FirecrawlClient`: process-global `aiolimiter`, the shared
`post_json` transient-retry loop, `aclose()`. The RT lane swallows a hard failure (ADR 0004),
so a raised transient blip is retried within the provider first.

**Content-format caveat:** Tavily `raw_content` is cleaned page text, not Firecrawl markdown.
The RT score/synopsis slicing in `rt.py` anchors on page *text* markers ("Tomatometer",
"Popcornmeter", "Movie Info", …), which survive in either shape — but this is the parity point
to confirm on the owner-run live check (see TASKS Phase 8).
"""

from __future__ import annotations

import logging

import httpx
from aiolimiter import AsyncLimiter

from .config import Settings
from .search import RTHit, hits_to_rt, post_json, rank_rt_hits

log = logging.getLogger(__name__)

_API = "https://api.tavily.com"


class TavilyClient:
    """Thin async wrapper over Tavily `/search` (scoped to rottentomatoes.com).

    Use as an async context manager so the HTTP connection pool is closed::

        async with TavilyClient(settings) as tv:
            candidates = await tv.search_rt_candidates("Dune: Part Two", "Movie")
    """

    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {settings.TAVILY_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        # One limiter per client instance; one shared TavilyClient per process → a process-
        # global throttle (≤ TAVILY_RPM/min) over the sweep + HITL resume (ADR 0013 — mirrors
        # FirecrawlClient._limiter).
        self._limiter = AsyncLimiter(settings.TAVILY_RPM, 60)
        self._max_retries = settings.RETRY_MAX_ATTEMPTS

    async def __aenter__(self) -> TavilyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _search(self, query: str, limit: int = 5) -> list[dict]:
        """POST /search scoped to rottentomatoes.com, returning `{url, title, markdown}` dicts.

        `include_raw_content` pulls each hit's full page text (the content the extractor needs)
        and `include_domains` scopes discovery to RT. Goes through the shared `post_json`
        transient-retry loop; a non-transient error / exhausted retries raises (caught above).
        """
        body = {
            "query": query,
            "include_domains": ["rottentomatoes.com"],
            "include_raw_content": True,
            "max_results": limit,
            "search_depth": "basic",
        }
        data = await post_json(
            self._client,
            "/search",
            body,
            limiter=self._limiter,
            max_retries=self._max_retries,
        )
        results = data.get("results") or []
        return [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                # raw_content is the full page text; content is a short snippet — prefer raw.
                "markdown": r.get("raw_content") or r.get("content", ""),
            }
            for r in results
        ]

    async def search_rt_candidates(
        self, title: str, media_type: str | None = None
    ) -> list[RTHit]:
        """Find a title's canonical RT pages, ranked best-first (empty on a soft miss).

        Same contract as `FirecrawlClient.search_rt_candidates` (the `SearchClient` Protocol):
        title-only query (domain scoped via `include_domains`), ranked canonical `RTHit`s each
        carrying identity + inline content. Empty list = soft miss; a hard failure raises.
        """
        hits = await self._search(title)
        ranked = rank_rt_hits(hits, media_type)
        if not ranked:
            log.info("tavily: no RT page for %r in %d hits — soft miss", title, len(hits))
        return hits_to_rt(ranked)

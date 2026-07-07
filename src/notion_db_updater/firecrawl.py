"""Async Firecrawl client — the RT lane's primary provider (ADR 0003, Phase 4).

In production we start from a *title*, not a URL, and RT slugs aren't derivable from a title
(`/m/parasite_2019`, `/tv/the_last_of_us`). So the flow DISCOVERS the page (proven 4/4 by
`spikes/05_firecrawl_rt_extraction.py`):

    search "<title> site:rottentomatoes.com"  → pick the canonical /m/ or /tv/ hit
    → use the markdown Firecrawl scraped inline (one call) → hand to the LLM extractor

Thin httpx wrapper (mirrors `omdb.py`) rather than the Firecrawl SDK — the codebase rolls its
own clients, and the spike proved the raw `/search` shape. Pinned to the **v2** API (proven
4/4 by the spike; the live sweep hits `/v2/search`). `maxAge` makes repeat scrapes within a
week hit Firecrawl's cache.

**Year caveat:** the RT lane runs *concurrently* with OMDb (Phase 4 fan-out), so it cannot
use OMDb's resolved year, and the Watchlist Entry carries none (§8). The query is therefore
title-only; `media_type` biases the `/m/` vs `/tv/` pick instead. Some titles will soft-miss
without a year — acceptable for the thin slice (Phase 8 fallbacks + the Phase 5 Judge backstop
this); the spike's 4/4 was *with* year.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from aiolimiter import AsyncLimiter

from .config import Settings
from .resilience import TRANSIENT_STATUS
from .search import RTHit, hits_to_rt, rank_rt_hits

log = logging.getLogger(__name__)

_API = "https://api.firecrawl.dev/v2"

# maxAge (ms): serve from Firecrawl's cache if the page was scraped < 1 week ago.
_WEEK_MS = 7 * 24 * 60 * 60 * 1000


class FirecrawlClient:
    """Thin async wrapper over Firecrawl `/search` (scoped to rottentomatoes.com).

    Use as an async context manager so the HTTP connection pool is closed::

        async with FirecrawlClient(settings) as fc:
            candidates = await fc.search_rt_candidates("Dune: Part Two", "Movie")
    """

    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        # One limiter per client instance; there is one shared FirecrawlClient per process, so
        # this is effectively a process-global throttle (≤ FIRECRAWL_RPM/min) over the sweep
        # and HITL resume alike (ADR 0013 — mirrors NotionClient._limiter).
        self._limiter = AsyncLimiter(settings.FIRECRAWL_RPM, 60)
        self._max_retries = settings.RETRY_MAX_ATTEMPTS

    async def __aenter__(self) -> FirecrawlClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _retry_delay(exc: httpx.HTTPStatusError | None, attempt: int) -> float:
        """Seconds to wait before the next retry: server `Retry-After` on a 429, else backoff.

        Exponential backoff (`0.5·2^attempt`, capped at 30s) mirrors the node RetryPolicy's
        defaults; a 429's `Retry-After` header wins when present (honored like `NotionClient`).
        """
        if exc is not None and exc.response.status_code == 429:
            try:
                return float(exc.response.headers.get("Retry-After", "1"))
            except ValueError:
                pass
        return min(0.5 * 2**attempt, 30.0)

    async def _search(self, query: str, limit: int = 5) -> list[dict]:
        """POST /v2/search, scraping each hit to markdown inline.

        v2 groups results by source (`sources: ["web"]` → `{"data": {"web": [...]}}`). Each
        attempt acquires a limiter slot (≤ FIRECRAWL_RPM/min); a **transient** failure (429 /
        5xx / transport) is retried up to `RETRY_MAX_ATTEMPTS`, honoring `Retry-After` on 429
        with exponential backoff otherwise. A non-transient error (a 4xx, a parse failure) or
        exhausted retries raises — the RT subgraph swallows that as best-effort (ADR 0004 /
        0013), so this "retry within the provider" never leaks a transient blip up to fail the
        Entry. Structurally mirrors `NotionClient._request`.
        """
        body = {
            "query": query,
            "limit": limit,
            "sources": ["web"],
            "scrapeOptions": {
                "formats": ["markdown"],
                "onlyMainContent": True,
                "maxAge": _WEEK_MS,
            },
        }
        for attempt in range(self._max_retries):
            try:
                async with self._limiter:
                    resp = await self._client.post("/search", json=body)
                resp.raise_for_status()
            except httpx.TransportError:
                if attempt + 1 >= self._max_retries:
                    raise
                await asyncio.sleep(self._retry_delay(None, attempt))
                continue
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code not in TRANSIENT_STATUS
                    or attempt + 1 >= self._max_retries
                ):
                    raise
                await asyncio.sleep(self._retry_delay(exc, attempt))
                continue
            hits = resp.json().get("data", {}).get("web") or []
            return [
                {
                    "url": h.get("url", ""),
                    "title": h.get("title", ""),
                    "markdown": h.get("markdown", ""),
                }
                for h in hits
            ]
        raise AssertionError("unreachable: loop returns or raises")  # pragma: no cover

    async def search_rt_candidates(
        self, title: str, media_type: str | None = None
    ) -> list[RTHit]:
        """Find a title's canonical RT pages, ranked best-first (empty on a soft miss).

        Title-only query (see the module note on the year caveat). Returns the ranked canonical
        `RTHit`s — each carrying identity (`url`/`title`/`year`) and the inline-scraped
        `markdown` (already paid for). The RT subgraph takes `[0]` on the common single-match
        fast path; Phase 5's `resolve_rt` correlates the set against OMDb when >1 is in
        contention. An empty list is a soft miss (no RT page), distinct from a hard failure
        (which raises — the caller decides what that means; ADR 0004).
        """
        query = f"{title} site:rottentomatoes.com"
        hits = await self._search(query)
        ranked = rank_rt_hits(hits, media_type)
        if not ranked:
            log.info("firecrawl: no RT page for %r in %d hits — soft miss", title, len(hits))
        return hits_to_rt(ranked)

"""Async Exa client — an RT search provider peer of Firecrawl (Phase 8, ADR 0003 amended).

A full `SearchClient` (search + inline page content + the same candidate shaping): Exa's
`/search` scopes to rottentomatoes.com via `includeDomains` and returns each hit's page text
via `contents: {text: true}`, feeding the shared `rank_rt_hits` → `RTHit` pipeline. Parity with
Firecrawl / Tavily is what makes the round-robin rotation safe (ADR 0003).

Thin httpx wrapper, mirroring `FirecrawlClient`: process-global `aiolimiter`, the shared
`post_json` transient-retry loop, `aclose()`. Auth is Exa's `x-api-key` header (not Bearer).

**Content-format caveat:** Exa `text` is plain extracted page text, not Firecrawl markdown; the
RT score/synopsis markers survive in text — confirm on the owner-run live check (TASKS §8).
"""

from __future__ import annotations

import logging

import httpx
from aiolimiter import AsyncLimiter

from .config import Settings
from .search import RTHit, hits_to_rt, post_json, rank_rt_hits

log = logging.getLogger(__name__)

_API = "https://api.exa.ai"


class ExaClient:
    """Thin async wrapper over Exa `/search` (scoped to rottentomatoes.com).

    Use as an async context manager so the HTTP connection pool is closed::

        async with ExaClient(settings) as ex:
            candidates = await ex.search_rt_candidates("Dune: Part Two", "Movie")
    """

    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            base_url=_API,
            headers={
                "x-api-key": settings.EXA_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        # Process-global throttle (≤ EXA_RPM/min); one shared ExaClient/process (ADR 0013).
        self._limiter = AsyncLimiter(settings.EXA_RPM, 60)
        self._max_retries = settings.RETRY_MAX_ATTEMPTS

    async def __aenter__(self) -> ExaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _search(self, query: str, limit: int = 5) -> list[dict]:
        """POST /search scoped to rottentomatoes.com, returning `{url, title, markdown}` dicts.

        `contents.text` pulls each hit's page text (the content the extractor needs);
        `includeDomains` scopes discovery to RT. Goes through the shared `post_json` transient-
        retry loop; a non-transient error / exhausted retries raises (swallowed upstream).
        """
        body = {
            "query": query,
            "includeDomains": ["rottentomatoes.com"],
            "numResults": limit,
            "type": "auto",
            "contents": {"text": True},
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
                "markdown": r.get("text", ""),
            }
            for r in results
        ]

    async def search_rt_candidates(
        self, title: str, media_type: str | None = None
    ) -> list[RTHit]:
        """Find a title's canonical RT pages, ranked best-first (empty on a soft miss).

        Same contract as the other `SearchClient`s: title-only query (domain scoped via
        `includeDomains`), ranked canonical `RTHit`s each carrying identity + inline content.
        Empty list = soft miss; a hard failure raises.
        """
        hits = await self._search(title)
        ranked = rank_rt_hits(hits, media_type)
        if not ranked:
            log.info("exa: no RT page for %r in %d hits — soft miss", title, len(hits))
        return hits_to_rt(ranked)

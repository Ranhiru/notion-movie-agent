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
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from aiolimiter import AsyncLimiter

from .config import Settings
from .resilience import TRANSIENT_STATUS

log = logging.getLogger(__name__)

_API = "https://api.firecrawl.dev/v2"

# maxAge (ms): serve from Firecrawl's cache if the page was scraped < 1 week ago.
_WEEK_MS = 7 * 24 * 60 * 60 * 1000

# RT slugs often carry a disambiguating year suffix: /m/parasite_2019, /tv/dune_prophecy.
_SLUG_YEAR = re.compile(r"_(\d{4})$")


@dataclass(frozen=True, slots=True)
class RTHit:
    """One canonical Rotten Tomatoes title page discovered by the RT lane.

    Carries the page *identity* (`url`, `title`, `year`) the Judge needs to correlate against
    OMDb's resolved identity (ADR 0003 / 0008), plus the `markdown` Firecrawl scraped inline
    (already paid for) so score extraction can run without a second fetch. `year` is parsed
    from the RT slug (`/m/parasite_2019`) when present, else None.
    """

    url: str
    title: str
    year: int | None
    markdown: str | None


def _slug_year(url: str) -> int | None:
    """Parse the trailing `_YYYY` year off an RT slug, or None when absent."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    match = _SLUG_YEAR.search(slug)
    return int(match.group(1)) if match else None


def rank_rt_hits(hits: list[dict], media_type: str | None = None) -> list[dict]:
    """Rank the canonical Rotten Tomatoes title pages among search hits, best first.

    Keeps only bare `/m/<slug>` or `/tv/<slug>` pages (2 path segments), dropping deep links
    like `/m/<slug>/reviews`. When `media_type` is known, biases toward the matching path
    (`Movie` → `/m/`, `TV Show` → `/tv/`) — the parallel RT lane's only disambiguator, since it
    has no year. Returns the ranked list (empty on a soft miss); Phase 5's `resolve_rt`
    correlates the top-N against OMDb's identity when more than one is in contention.
    """
    preferred = {"Movie": "/m/", "TV Show": "/tv/"}.get(media_type or "")
    scored: list[tuple[int, dict]] = []
    for h in hits:
        parsed = urlparse(h.get("url", ""))
        path = parsed.path
        if not parsed.netloc.lower().endswith("rottentomatoes.com"):
            continue
        if not (path.startswith("/m/") or path.startswith("/tv/")):
            continue
        canonical = path.rstrip("/").count("/") == 2  # /m/slug, not /m/slug/reviews
        if not canonical:
            continue  # only canonical title pages are candidates (deep links can't score)
        on_type = preferred is not None and path.startswith(preferred)
        # Lower rank sorts first: matching media type wins.
        scored.append((0 if on_type else 1, h))
    scored.sort(key=lambda t: t[0])
    return [h for _, h in scored]


def pick_rt_hit(hits: list[dict], media_type: str | None = None) -> dict | None:
    """The single best canonical RT page (deterministic fast path), or None on a soft miss."""
    ranked = rank_rt_hits(hits, media_type)
    return ranked[0] if ranked else None


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
        return [
            RTHit(
                url=h.get("url", ""),
                title=h.get("title", ""),
                year=_slug_year(h.get("url", "")),
                markdown=h.get("markdown") or None,
            )
            for h in ranked
        ]

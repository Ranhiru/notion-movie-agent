"""Async Firecrawl client — the RT lane's primary provider (ADR 0003, Phase 4).

In production we start from a *title*, not a URL, and RT slugs aren't derivable from a title
(`/m/parasite_2019`, `/tv/the_last_of_us`). So the flow DISCOVERS the page (proven 4/4 by
`spikes/05_firecrawl_rt_extraction.py`):

    search "<title> site:rottentomatoes.com"  → pick the canonical /m/ or /tv/ hit
    → use the markdown Firecrawl scraped inline (one call) → hand to the LLM extractor

Thin httpx wrapper (mirrors `omdb.py`) rather than the Firecrawl SDK — the codebase rolls its
own clients, and the spike proved the raw `/search` shape. v2→v1 base fallback is kept from
the spike. `maxAge` makes repeat scrapes within a week hit Firecrawl's cache.

**Year caveat:** the RT lane runs *concurrently* with OMDb (Phase 4 fan-out), so it cannot
use OMDb's resolved year, and the Watchlist Entry carries none (§8). The query is therefore
title-only; `media_type` biases the `/m/` vs `/tv/` pick instead. Some titles will soft-miss
without a year — acceptable for the thin slice (Phase 8 fallbacks + the Phase 5 Judge backstop
this); the spike's 4/4 was *with* year.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# maxAge (ms): serve from Firecrawl's cache if the page was scraped < 1 week ago.
_WEEK_MS = 7 * 24 * 60 * 60 * 1000


def _bases(override: str) -> list[str]:
    """Firecrawl API base(s): an explicit override, else v2 with a v1 fallback (spike 05)."""
    return (
        [override]
        if override
        else ["https://api.firecrawl.dev/v2", "https://api.firecrawl.dev/v1"]
    )


def pick_rt_hit(hits: list[dict], media_type: str | None = None) -> dict | None:
    """Choose the canonical Rotten Tomatoes title page from search hits.

    Prefers a bare `/m/<slug>` or `/tv/<slug>` (2 path segments) over deep links like
    `/m/<slug>/reviews`. When `media_type` is known, biases toward the matching path
    (`Movie` → `/m/`, `TV Show` → `/tv/`) — the parallel RT lane's only disambiguator, since
    it has no year. Returns the best hit, or None when no RT page is present (a soft miss).
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
        on_type = preferred is not None and path.startswith(preferred)
        # Lower rank sorts first: matching media type beats canonical-shape beats the rest.
        rank = (0 if on_type else 1, 0 if canonical else 1)
        scored.append((rank[0] * 2 + rank[1], h))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0])
    return scored[0][1]


class FirecrawlClient:
    """Thin async wrapper over Firecrawl `/search` (scoped to rottentomatoes.com).

    Use as an async context manager so the HTTP connection pool is closed::

        async with FirecrawlClient(settings) as fc:
            markdown = await fc.search_rt("Dune: Part Two", "Movie")
    """

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.FIRECRAWL_API_KEY
        self._bases = _bases(getattr(settings, "FIRECRAWL_API_URL", "") or "")
        self._client = httpx.AsyncClient(timeout=120.0)

    async def __aenter__(self) -> FirecrawlClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def _search(self, query: str, limit: int = 5) -> list[dict]:
        """POST /search, scraping each hit to markdown inline. v2→v1 fallback (spike 05)."""
        scrape_opts = {"formats": ["markdown"], "onlyMainContent": True, "maxAge": _WEEK_MS}
        last_err: str | None = None
        for base in self._bases:
            body: dict = {"query": query, "limit": limit, "scrapeOptions": scrape_opts}
            if base.endswith("/v2"):
                body["sources"] = ["web"]  # v2 groups results by source; v1 rejects this key
            try:
                resp = await self._client.post(
                    f"{base}/search", headers=self._headers(), json=body
                )
                if resp.status_code == 404:  # wrong API version — try the next base
                    last_err = f"404 at {base}"
                    continue
                resp.raise_for_status()
                data = resp.json().get("data", {})
                if isinstance(data, dict):  # v2: {"web": [...]}
                    hits = data.get("web") or data.get("results") or []
                elif isinstance(data, list):  # v1: flat list
                    hits = data
                else:
                    hits = []
                return [
                    {
                        "url": h.get("url", ""),
                        "title": h.get("title", ""),
                        "markdown": h.get("markdown", ""),
                    }
                    for h in hits
                ]
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__} at {base}: {e}"
        raise RuntimeError(f"firecrawl search failed for {query!r}: {last_err}")

    async def search_rt(self, title: str, media_type: str | None = None) -> str | None:
        """Find a title's Rotten Tomatoes page and return its markdown, or None on a soft miss.

        Title-only query (see the module note on the year caveat). Returns None when no RT page
        appears in the results — a soft miss, distinct from a hard failure (which raises). The
        caller (RT subgraph) decides what an exception means; here we only signal "no page".
        """
        query = f"{title} site:rottentomatoes.com"
        hits = await self._search(query)
        hit = pick_rt_hit(hits, media_type)
        if not hit:
            log.info("firecrawl: no RT page for %r in %d hits — soft miss", title, len(hits))
            return None
        markdown = hit.get("markdown") or None
        if not markdown:
            log.info(
                "firecrawl: RT page %s had no inline markdown — soft miss", hit.get("url")
            )
        return markdown

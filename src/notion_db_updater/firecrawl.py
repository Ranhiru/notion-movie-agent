"""Async Firecrawl client for RT enrichment and conversational web research.

Conversation uses general `/search` results and `/scrape` markdown. The same client and
rate limiter serve both flows; supplied URLs are never fetched by the bot directly.

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
import ipaddress
import logging
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx
from aiolimiter import AsyncLimiter

from .config import Settings
from .search import RTHit, hits_to_rt, post_json, rank_rt_hits

log = logging.getLogger(__name__)

_API = "https://api.firecrawl.dev/v2"

# maxAge (ms): serve from Firecrawl's cache if the page was scraped < 1 week ago.
_WEEK_MS = 7 * 24 * 60 * 60 * 1000


def _validated_url(url: str) -> tuple[str, str, int, str]:
    """Return a normalized URL target, rejecting ambiguous or unsafe URL forms."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("URL is empty or too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url) or "\\" in url:
        raise ValueError("URL contains forbidden characters")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if (
        scheme not in {"http", "https"}
        or not host
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Only public HTTP and HTTPS URLs are allowed")
    expected_port = 443 if scheme == "https" else 80
    if port not in (None, expected_port):
        raise ValueError("Non-standard URL ports are not allowed")
    if "%" in host:  # Reject IPv6 zone identifiers and encoded host ambiguity.
        raise ValueError("Scoped or encoded host names are not allowed")

    try:
        normalized_host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            normalized_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("URL host name is invalid") from exc
        if (
            not normalized_host
            or normalized_host == "localhost"
            or normalized_host.endswith(".localhost")
        ):
            raise ValueError("Local host names are not allowed") from None

    path = parsed.path or "/"
    target = urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))
    return target, scheme, expected_port, normalized_host


async def _public_addresses(host: str, port: int) -> list[str]:
    """Resolve once and reject the whole answer if any address is not globally routable."""
    try:
        literal = ipaddress.ip_address(host)
        candidates = [str(literal)]
    except ValueError:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        candidates = [record[4][0] for record in records]
    addresses: list[str] = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError("DNS returned an invalid address") from exc
        if not address.is_global:
            raise ValueError("URL resolves to a non-public network address")
        value = str(address)
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise ValueError("URL host has no public addresses")
    return addresses


class FirecrawlClient:
    """Async Firecrawl search and scraping for RT enrichment and conversation research.

    Use as an async context manager so the HTTP connection pool is closed::

        async with FirecrawlClient(settings) as fc:
            candidates = await fc.search_rt_candidates("Dune: Part Two", "Movie")
    """

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self._owns_client = http is None
        self._client = http or httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
            trust_env=False,
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
        if self._owns_client:
            await self._client.aclose()

    async def _search(self, query: str, limit: int = 5) -> list[dict]:
        """POST /v2/search, scraping each hit to markdown inline.

        v2 groups results by source (`sources: ["web"]` → `{"data": {"web": [...]}}`). The
        request goes through the shared `post_json` transient-retry loop (429 / 5xx / transport
        retried within the provider, honoring `Retry-After`); a non-transient error or exhaust
        raises, which the RT subgraph swallows as best-effort (ADR 0004 / 0013).
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
        data = await post_json(
            self._client,
            "/search",
            body,
            limiter=self._limiter,
            max_retries=self._max_retries,
        )
        hits = data.get("data", {}).get("web") or []
        return [
            {
                "url": h.get("url", ""),
                "title": h.get("title", ""),
                "markdown": h.get("markdown", ""),
            }
            for h in hits
        ]

    async def search(self, query: str) -> list[dict[str, str]]:
        """Return up to five general web results without paying for inline scrapes."""
        data = await post_json(
            self._client,
            "/search",
            {"query": query, "limit": 5, "sources": ["web"]},
            limiter=self._limiter,
            max_retries=self._max_retries,
            max_response_bytes=1_000_000,
        )
        if data.get("success") is not True:
            raise ValueError("Firecrawl search failed")
        return [
            {
                "url": str(hit.get("url") or ""),
                "title": str(hit.get("title") or ""),
                "content": str(hit.get("description") or ""),
            }
            for hit in (data.get("data", {}).get("web") or [])[:5]
            if isinstance(hit, dict)
        ]

    async def fetch_page(self, url: str) -> str:
        """Scrape a public URL remotely; no local connection to user-supplied pages."""
        normalized, _, port, host = _validated_url(url)
        await _public_addresses(host, port)
        data = await post_json(
            self._client,
            "/scrape",
            {
                "url": normalized,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "skipTlsVerification": False,
                "maxAge": _WEEK_MS,
            },
            limiter=self._limiter,
            max_retries=self._max_retries,
            max_response_bytes=1_000_000,
        )
        if data.get("success") is not True or not isinstance(data.get("data"), dict):
            raise ValueError("Firecrawl page retrieval failed")
        page = data["data"]
        metadata = page.get("metadata") or {}
        if metadata.get("error") or metadata.get("statusCode", 200) >= 400:
            raise ValueError("Firecrawl could not read the supplied page")
        # Firecrawl owns redirect handling; reject non-public final destinations too.
        final_url = metadata.get("url") or metadata.get("sourceURL")
        if not final_url:
            raise ValueError("Firecrawl did not report the page URL")
        _, _, final_port, final_host = _validated_url(final_url)
        await _public_addresses(final_host, final_port)
        markdown = page.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("Firecrawl returned no page content")
        return markdown

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

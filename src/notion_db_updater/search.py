"""The RT search seam — the `SearchClient` strategy and its provider-agnostic helpers.

The RT lane discovers a title's canonical Rotten Tomatoes page by *searching* (it shares no
key with the OMDb lane — see `firecrawl.py`'s module note on the year caveat). ADR 0003
(amended) makes that discovery an injected **strategy**: the RT subgraph keeps a single
`rt_search` node that takes any `SearchClient`, so providers swap — and the Phase 8 rotation
chain grows — without touching graph topology.

This module owns the pieces every provider shares:

- **`RTHit`** — one canonical RT title page (identity + inline-scraped markdown).
- **`SearchClient`** — the one-method Protocol the `rt_search` node depends on. Search-only:
  lifecycle (open/close) lives on the concrete clients, which `Runtime` constructs and closes.
- **`rank_rt_hits` / `pick_rt_hit` / `_slug_year`** — provider-agnostic ranking + slug parsing,
  reused by every concrete client so candidate shaping is identical (the parity ADR 0003
  requires for rotation to be safe).

The concrete clients (`FirecrawlClient`, and the Phase 8 `TavilyClient` / `ExaClient`) and the
`RoundRobinSearchClient` composite all satisfy `SearchClient` identically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

# RT slugs often carry a disambiguating year suffix: /m/parasite_2019, /tv/dune_prophecy.
_SLUG_YEAR = re.compile(r"_(\d{4})$")


@dataclass(frozen=True, slots=True)
class RTHit:
    """One canonical Rotten Tomatoes title page discovered by the RT lane.

    Carries the page *identity* (`url`, `title`, `year`) the Judge needs to correlate against
    OMDb's resolved identity (ADR 0003 / 0008), plus the `markdown` the provider scraped inline
    (already paid for) so score extraction can run without a second fetch. `year` is parsed
    from the RT slug (`/m/parasite_2019`) when present, else None.
    """

    url: str
    title: str
    year: int | None
    markdown: str | None


@runtime_checkable
class SearchClient(Protocol):
    """The RT search strategy the `rt_search` node depends on (ADR 0003, amended).

    One method: given a title (and optional media type), return the ranked canonical RT pages,
    each a markdown-bearing `RTHit`. A single provider client (Firecrawl / Tavily / Exa) or the
    round-robin composite of them satisfies this identically — that parity is what lets the
    provider chain grow without changing graph topology. Search-only: the concrete clients own
    their own async lifecycle, managed by `Runtime`.
    """

    async def search_rt_candidates(
        self, title: str, media_type: str | None = None
    ) -> list[RTHit]:
        """Find a title's canonical RT pages, ranked best-first (empty on a soft miss)."""
        ...


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

    Provider-agnostic: every concrete `SearchClient` maps its raw results into `{url, title,
    markdown}` dicts and runs them through here, so candidate shaping is identical across the
    rotation chain (ADR 0003 parity).
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


def hits_to_rt(ranked: list[dict]) -> list[RTHit]:
    """Map ranked `{url, title, markdown}` result dicts into `RTHit`s (year parsed from slug).

    Shared tail of every concrete client's `search_rt_candidates`: once a provider has mapped
    its raw results into the common dict shape and ranked them, this produces the identical
    `RTHit` list the whole chain returns.
    """
    return [
        RTHit(
            url=h.get("url", ""),
            title=h.get("title", ""),
            year=_slug_year(h.get("url", "")),
            markdown=h.get("markdown") or None,
        )
        for h in ranked
    ]

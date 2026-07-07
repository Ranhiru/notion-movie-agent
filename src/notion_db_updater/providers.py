"""RT search-provider assembly — turn `SEARCH_PROVIDERS` into a live `SearchClient` (Phase 8).

The one place that maps provider *names* to concrete clients and decides bare-vs-rotation, kept
out of `search.py` (which must not import the concrete clients — they import *it*). `Runtime`
and the `--enrich`/`--generate-graph` CLI paths both wire the RT lane through here.

`build_search_client` is an async context manager: it constructs the clients named in
`SEARCH_PROVIDERS`, yields a single `SearchClient` (the bare client when one is configured, a
`RoundRobinSearchClient` composite when several — ADR 0003), and closes every concrete client
on exit. So the caller manages one lifecycle regardless of how many providers are active.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from .config import Settings
from .exa import ExaClient
from .firecrawl import FirecrawlClient
from .search import RoundRobinSearchClient, SearchClient
from .tavily import TavilyClient

log = logging.getLogger(__name__)

# Provider name (as it appears in SEARCH_PROVIDERS) → its concrete client constructor.
_PROVIDER_CLIENTS = {
    "firecrawl": FirecrawlClient,
    "tavily": TavilyClient,
    "exa": ExaClient,
}


@contextlib.asynccontextmanager
async def build_search_client(settings: Settings) -> AsyncIterator[SearchClient]:
    """Yield the RT `SearchClient` for `SEARCH_PROVIDERS`, closing its clients on exit.

    One configured provider → that bare client; several → a `RoundRobinSearchClient` that
    rotates the lead per Entry (ADR 0003). Unknown names are logged and skipped; if nothing
    valid remains, falls back to a bare `FirecrawlClient` (Firecrawl is the always-present
    primary from Phase 4). Every concrete client built here is closed when the context exits,
    so callers manage a single lifecycle.
    """
    named: list[tuple[str, SearchClient]] = []
    for name in settings.search_providers:
        client_cls = _PROVIDER_CLIENTS.get(name)
        if client_cls is None:
            log.warning("search: unknown provider %r in SEARCH_PROVIDERS — skipping", name)
            continue
        named.append((name, client_cls(settings)))

    if not named:
        log.warning("search: no valid providers configured — defaulting to firecrawl")
        named = [("firecrawl", FirecrawlClient(settings))]

    if len(named) == 1:
        log.info("rt_search: single provider %s (no rotation)", named[0][0])
        search: SearchClient = named[0][1]
    else:
        log.info("rt_search: rotating over %s", ", ".join(name for name, _ in named))
        search = RoundRobinSearchClient(named)

    try:
        yield search
    finally:
        for _, client in named:
            await client.aclose()  # type: ignore[attr-defined]  # concrete clients have aclose

"""Async Notion client for the Watchlist data source (API 2025-09-03).

Reads: `query_entries()` (the reconcile sweep query) and `get_entry(page_id)` (single page).
Write: `update_entry()` (idempotent upsert by `page_id`, Phase 2). Every request goes through
`_request()`, which applies a process-global `aiolimiter` throttle (≤ `NOTION_RPS`/s) and
retries 429s honoring `Retry-After` — the rate-limit guard pulled forward to Phase 3 (ADR
0001). Built async-first because everything downstream (LangGraph, the limiter, HITL resume)
is async; doing it now avoided a sync→async port.

Query/filter/write shapes were proven live by `spikes/01_notion_data_source.py`.
"""

from __future__ import annotations

import asyncio

import httpx
from aiolimiter import AsyncLimiter

from .config import NOTION_VERSION, Settings
from .models import PROP_STATUS, PROP_TITLE, Entry, enrichment_properties

_API = "https://api.notion.com/v1"

# Bounded 429 retries inside the client (honoring `Retry-After`). This is the Notion-
# specific transient guard pulled forward to Phase 3 (ADR 0001); the graph-level
# `RetryPolicy` for other transient errors arrives in Phase 7.
_MAX_429_RETRIES = 3

# The reconcile filter (RESEARCH §8, proven by spike 01): an Entry needs enrichment when
# Enrichment Status is empty (never run) OR equals "pending" (queued / retry).
_RECONCILE_FILTER = {
    "or": [
        {"property": PROP_STATUS, "select": {"is_empty": True}},
        {"property": PROP_STATUS, "select": {"equals": "pending"}},
    ]
}

# Phase 6d: the stale-interrupt pass queries rows paused at HITL disambiguation. Kept
# *separate* from the reconcile filter on purpose — folding `awaiting_input` into the sweep
# would make it re-run paused rows and break the status-partitioning that keeps the sweep and
# an out-of-band resume from touching the same row (ADR 0006).
_AWAITING_INPUT_FILTER = {"property": PROP_STATUS, "select": {"equals": "awaiting_input"}}


class NotionClient:
    """Thin async wrapper over the Notion data_sources/pages REST API.

    Use as an async context manager so the underlying HTTP connection pool is closed::

        async with NotionClient(settings) as notion:
            entries = await notion.query_entries()
    """

    def __init__(self, settings: Settings) -> None:
        self._data_source_id = settings.NOTION_WATCHLIST_DATA_SOURCE_ID
        self._client = httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {settings.NOTION_MOVIE_DB_TOKEN}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        # One limiter per client instance; there is one shared NotionClient per process,
        # so this is effectively a process-global throttle (≤ NOTION_RPS req/s) covering
        # the sweep and — from Phase 6 — HITL resume alike (ADR 0001).
        self._limiter = AsyncLimiter(settings.NOTION_RPS, 1)

    @property
    def data_source_id(self) -> str:
        return self._data_source_id

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Send one Notion request through the rate limiter, retrying 429s.

        Every call acquires a limiter slot first (≤ NOTION_RPS/s). On a 429 we sleep for
        the server-advised `Retry-After` (seconds; default 1s) and retry up to
        ``_MAX_429_RETRIES`` times; any other non-2xx raises via `raise_for_status()`.
        """
        for attempt in range(_MAX_429_RETRIES + 1):
            async with self._limiter:
                resp = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                try:
                    delay = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        raise AssertionError("unreachable: loop returns or raises")  # pragma: no cover

    async def _query_page(self, filter_: dict, cursor: str | None = None) -> dict:
        """Run one query page for the given filter; return the raw Notion response."""
        body: dict = {"filter": filter_, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = await self._request(
            "POST", f"/data_sources/{self._data_source_id}/query", json=body
        )
        return resp.json()

    async def _query_all(self, filter_: dict) -> list[Entry]:
        """Page through every row matching `filter_` and parse each into an Entry.

        Notion caps `page_size` at 100 and returns `has_more`/`next_cursor`; the backfill is
        ~100 rows, so this may span a couple of pages.
        """
        entries: list[Entry] = []
        cursor: str | None = None
        while True:
            data = await self._query_page(filter_, cursor)
            entries.extend(Entry.from_page(p) for p in data.get("results", []))
            # Guard against an infinite loop: stop if Notion claims more pages but
            # omits the cursor to fetch them.
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return entries

    async def query_entries(self) -> list[Entry]:
        """Return every Entry needing enrichment (Status empty OR pending)."""
        return await self._query_all(_RECONCILE_FILTER)

    async def query_awaiting_input(self) -> list[Entry]:
        """Return every Entry paused at HITL disambiguation (Status == awaiting_input).

        The Phase 6d stale-interrupt pass (ADR 0006): the cron scans these for rows that have
        sat unanswered past the timeout and auto-resolves them with the pre-filter's guess.
        """
        return await self._query_all(_AWAITING_INPUT_FILTER)

    async def get_entry(self, page_id: str) -> Entry:
        """Fetch and parse a single Entry by its Notion page id."""
        resp = await self._request("GET", f"/pages/{page_id}")
        return Entry.from_page(resp.json())

    async def find_by_title(self, title: str) -> Entry | None:
        """Best-effort dedupe (Phase 9 / ADR 0012): an existing Entry with this exact title.

        Queries with a title `contains` filter (Notion text filters are case-insensitive) —
        deliberately with *no* status filter, since a `done` row (which the reconcile filter
        skips) must still count as a duplicate. `contains` can over-match a substring
        (*Dune* ⊂ *Dune: Part Two*), so the final match requires case-insensitive **exact**
        equality: this catches an exact re-add but not variant spellings, exactly as ADR 0012
        scopes dedupe. Returns the first exact match, or None.
        """
        key = title.strip().lower()
        if not key:
            return None
        filter_ = {"property": PROP_TITLE, "title": {"contains": title.strip()}}
        for entry in await self._query_all(filter_):
            if (entry.title or "").strip().lower() == key:
                return entry
        return None

    async def create_entry(self, title: str) -> Entry:
        """Create a new Watchlist page (Phase 9 / ADR 0012): Entry only, `Type` blank, pending.

        The `/add` path originates a row exactly as a human would — just a Title and
        `Enrichment Status = pending` — then enriches that `page_id` out-of-band through the
        same graph. `Type` is left blank on purpose (search OMDb unfiltered, resolve
        `media_type` via the 1/many + disambiguation logic, then backfill it on write-back).

        This is the one Notion API shape spike 01 didn't exercise (it proved query + PATCH):
        `POST /v1/pages` with a `2025-09-03` **data_source** parent. Returns the created Entry.
        """
        body = {
            "parent": {"type": "data_source_id", "data_source_id": self._data_source_id},
            "properties": enrichment_properties(status="pending")
            | {PROP_TITLE: {"title": [{"text": {"content": title.strip()}}]}},
        }
        resp = await self._request("POST", "/pages", json=body)
        return Entry.from_page(resp.json())

    async def update_entry(self, page_id: str, properties: dict) -> Entry:
        """Write enrichment back to one page (idempotent upsert by `page_id`).

        `properties` is a Notion `properties` payload (build it with
        `models.enrichment_properties`). PATCH overwrites the named props in place, so
        re-running enrichment on the same page is a no-op-equivalent (ADR 0004 idempotency).
        Returns the re-parsed `Entry` from the write response.
        """
        resp = await self._request(
            "PATCH", f"/pages/{page_id}", json={"properties": properties}
        )
        return Entry.from_page(resp.json())

    async def query_raw(self) -> dict:
        """Return the raw first-page query JSON (for capturing the Phase 1 fixture).

        Separate from `query_entries()` so the parsed path stays clean; this mirrors the
        single request shape used to capture `tests/fixtures/notion_query.json`.
        """
        return await self._query_page(_RECONCILE_FILTER)

"""Async Notion client for the Watchlist data source (API 2025-09-03).

Phase 1 is read-only: `query_titles()` (the reconcile sweep query) and `get_title(page_id)`
(single page). Built async-first because everything downstream — LangGraph `max_concurrency`,
the Phase 3 `aiolimiter` Notion limiter, HITL resume — is async; doing it now avoids a
sync→async port later. The write path (idempotent upsert by `page_id`) arrives in Phase 2.

Query/filter/write shapes were proven live by `spikes/01_notion_data_source.py`.
"""

from __future__ import annotations

import httpx

from .config import NOTION_VERSION, Settings
from .models import PROP_STATUS, Title

_API = "https://api.notion.com/v1"

# The reconcile filter (RESEARCH §8, proven by spike 01): a Title needs enrichment when
# Enrichment Status is empty (never run) OR equals "pending" (queued / retry).
_RECONCILE_FILTER = {
    "or": [
        {"property": PROP_STATUS, "select": {"is_empty": True}},
        {"property": PROP_STATUS, "select": {"equals": "pending"}},
    ]
}


class NotionClient:
    """Thin async wrapper over the Notion data_sources/pages REST API.

    Use as an async context manager so the underlying HTTP connection pool is closed::

        async with NotionClient(settings) as notion:
            titles = await notion.query_titles()
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

    @property
    def data_source_id(self) -> str:
        return self._data_source_id

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _query_page(self, cursor: str | None = None) -> dict:
        """Run one reconcile-filter query page; return the raw Notion response."""
        body: dict = {"filter": _RECONCILE_FILTER, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = await self._client.post(
            f"/data_sources/{self._data_source_id}/query", json=body
        )
        resp.raise_for_status()
        return resp.json()

    async def query_titles(self) -> list[Title]:
        """Return every Title needing enrichment (Status empty OR pending).

        Pages through the full result set (the backfill is ~100 rows); Notion caps
        `page_size` at 100 and returns `has_more`/`next_cursor`.
        """
        titles: list[Title] = []
        cursor: str | None = None
        while True:
            data = await self._query_page(cursor)
            titles.extend(Title.from_page(p) for p in data.get("results", []))
            # Guard against an infinite loop: stop if Notion claims more pages but
            # omits the cursor to fetch them.
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return titles

    async def get_title(self, page_id: str) -> Title:
        """Fetch and parse a single Title by its Notion page id."""
        resp = await self._client.get(f"/pages/{page_id}")
        resp.raise_for_status()
        return Title.from_page(resp.json())

    async def query_raw(self) -> dict:
        """Return the raw first-page query JSON (for capturing the Phase 1 fixture).

        Separate from `query_titles()` so the parsed path stays clean; this mirrors the
        single request shape used to capture `tests/fixtures/notion_query.json`.
        """
        return await self._query_page()

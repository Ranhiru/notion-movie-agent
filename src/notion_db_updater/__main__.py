"""CLI entry points for the enrichment agent.

Phase 1 (default) — read the Watchlist and print Titles + property values:
    uv run python -m notion_db_updater
    uv run python -m notion_db_updater --capture-fixture   # also save the query JSON

Phase 2 — enrich ONE Title end-to-end (read_page → OMDb → write IMDb/plot/genre):
    uv run python -m notion_db_updater --enrich <page_id>
    uv run python -m notion_db_updater --enrich <page_id> --capture-fixtures  # + OMDb fixtures

Verification (TASKS.md Phase 2): a known row gains IMDB Rating + Plot Summary + Genre and
`Enrichment Status = done`; re-running is idempotent; a gibberish Title → `failed`. The
LangSmith trace shows read_page → omdb → update_notion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings, get_settings
from .graph import build_graph
from .models import EXPECTED_PROPERTIES, Title
from .notion import NotionClient
from .omdb import OMDbClient

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_FIXTURE_PATH = _FIXTURE_DIR / "notion_query.json"

# A query OMDb will never resolve — used to capture the 0-result fixture / branch.
_NONSENSE_TITLE = "zzqxwv no such title 1234567890"


def _print_title(t: Title) -> None:
    print(f"  • {t.title!r}" + (f"  [{t.media_type}]" if t.media_type else ""))
    print(f"      page_id            = {t.page_id}")
    print(f"      Enrichment Status  = {t.status}")
    print(f"      IMDB Rating        = {t.imdb_rating}")
    print(f"      RT Critic/Audience = {t.rt_critic} / {t.rt_audience}")
    print(f"      Genre              = {t.genre!r}")
    print(f"      Plot Summary       = {(t.plot or '')[:80]!r}")


def _check_property_drift(results: list[dict]) -> None:
    """Confirm the §8 property names are actually present on a real row (catches drift)."""
    if not results:
        print("\n  (no rows matched — cannot check property names)")
        return
    present = set(results[0].get("properties", {}).keys())
    print("\n  §8 property-name check (row 0):")
    for expected in EXPECTED_PROPERTIES:
        mark = "✓" if expected in present else "✗ MISSING"
        print(f"      {mark}  {expected!r}")


def _write_fixture(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  fixture written → {path}")


async def _read(capture_fixture: bool) -> None:
    settings = get_settings()
    async with NotionClient(settings) as notion:
        print(f"data_source_id = {notion.data_source_id}\n")
        raw = await notion.query_raw()
        results = raw.get("results", [])
        titles = [Title.from_page(p) for p in results]
        print(f"matched {len(titles)} Title(s) needing enrichment (empty OR pending):\n")
        for t in titles:
            _print_title(t)
        _check_property_drift(results)
        if capture_fixture:
            _write_fixture(_FIXTURE_PATH, raw)


async def _capture_omdb_fixtures(omdb: OMDbClient, title: str) -> None:
    """Snapshot raw OMDb responses (search hit + details + not-found) for offline tests."""
    print("\ncapturing OMDb fixtures…")
    search = await omdb.raw(s=title)
    _write_fixture(_FIXTURE_DIR / "omdb_search.json", search)
    hits = search.get("Search") or []
    if hits:
        details = await omdb.details(hits[0]["imdbID"])
        _write_fixture(_FIXTURE_DIR / "omdb_details.json", details)
    not_found = await omdb.raw(s=_NONSENSE_TITLE)
    _write_fixture(_FIXTURE_DIR / "omdb_not_found.json", not_found)


async def _enrich(page_id: str, capture_fixtures: bool) -> None:
    settings: Settings = get_settings()
    async with NotionClient(settings) as notion, OMDbClient(settings) as omdb:
        graph = build_graph(notion, omdb)

        graph.get_graph().draw_mermaid_png(output_file_path="flow.png")

        print(f"enriching page_id = {page_id}\n")
        final = await graph.ainvoke({"page_id": page_id})

        title: Title | None = final.get("title")
        print("result:")
        print(f"  Title              = {title.title!r}" if title else "  Title    = ?")
        print(f"  Enrichment Status  = {final.get('status')}")
        print(f"  IMDB id / rating   = {final.get('imdb_id')} / {final.get('imdb_rating')}")
        print(f"  Genre              = {final.get('genre')!r}")
        print(f"  Plot Summary       = {(final.get('plot') or '')[:80]!r}")
        if final.get("candidates") is not None:
            print(f"  OMDb candidates    = {len(final['candidates'])}")
        if final.get("note"):
            print(f"  note               = {final['note']}")

        if capture_fixtures and title and title.title:
            await _capture_omdb_fixtures(omdb, title.title)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enrich",
        metavar="PAGE_ID",
        help="run the OMDb enrichment graph for one Title (Phase 2)",
    )
    parser.add_argument(
        "--capture-fixture",
        action="store_true",
        help="(read mode) save the raw query response to tests/fixtures/notion_query.json",
    )
    parser.add_argument(
        "--capture-fixtures",
        action="store_true",
        help="(with --enrich) save raw OMDb search/details/not-found responses as fixtures",
    )
    args = parser.parse_args()

    if args.enrich:
        asyncio.run(_enrich(args.enrich, args.capture_fixtures))
    else:
        asyncio.run(_read(args.capture_fixture))


if __name__ == "__main__":
    main()

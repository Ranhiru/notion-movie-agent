"""Phase 1 entry: read the Watchlist and print Titles + property values.

Run:
    uv run python -m notion_db_updater          # print Titles needing enrichment
    uv run python -m notion_db_updater --capture-fixture  # also save the query JSON

Verification (TASKS.md Phase 1): real rows with correct §8 property names appear, the
data_source_id is confirmed, and the raw query response is captured to
tests/fixtures/notion_query.json for offline tests in later phases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import get_settings
from .models import EXPECTED_PROPERTIES, Title
from .notion import NotionClient

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "notion_query.json"
)


def _print_title(t: Title) -> None:
    print(f"  • {t.title!r}" + (f"  [{t.media_type}]" if t.media_type else ""))
    print(f"      page_id            = {t.page_id}")
    print(f"      Enrichment Status  = {t.status}")
    print(f"      IMDB Rating        = {t.imdb_rating}")
    print(f"      RT Critic/Audience = {t.rt_critic} / {t.rt_audience}")
    print(f"      Genre              = {t.genre!r}")
    print(f"      Plot Summary       = {(t.plot or '')[:80]!r}")


def _check_property_drift(raw: dict) -> None:
    """Confirm the §8 property names are actually present on a real row (catches drift)."""
    results = raw.get("results", [])
    if not results:
        print("\n  (no rows matched — cannot check property names)")
        return
    present = set(results[0].get("properties", {}).keys())
    print("\n  §8 property-name check (row 0):")
    for expected in EXPECTED_PROPERTIES:
        mark = "✓" if expected in present else "✗ MISSING"
        print(f"      {mark}  {expected!r}")


def _capture_fixture(raw: dict) -> None:
    _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FIXTURE_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    print(f"\n  fixture written → {_FIXTURE_PATH}")


async def _run(capture_fixture: bool) -> None:
    settings = get_settings()
    async with NotionClient(settings) as notion:
        print(f"data_source_id = {notion.data_source_id}\n")
        raw = await notion.query_raw()
        titles = [Title.from_page(p) for p in raw.get("results", [])]
        print(f"matched {len(titles)} Title(s) needing enrichment (empty OR pending):\n")
        for t in titles:
            _print_title(t)
        _check_property_drift(raw)
        if capture_fixture:
            _capture_fixture(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture-fixture",
        action="store_true",
        help="save the raw query response to tests/fixtures/notion_query.json",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.capture_fixture))


if __name__ == "__main__":
    main()

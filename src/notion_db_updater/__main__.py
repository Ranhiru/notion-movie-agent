"""CLI entry points for the enrichment agent.

Phase 1 (default) — read the Watchlist and print entries + property values:
    uv run python -m notion_db_updater
    uv run python -m notion_db_updater --capture-fixture   # also save the query JSON

Phase 2 — enrich ONE Entry end-to-end (read_page → OMDb → write IMDb/plot/genre):
    uv run python -m notion_db_updater --enrich <page_id>
    uv run python -m notion_db_updater --enrich <page_id> --capture-fixtures  # + OMDb fixtures

Phase 3 — sweep the whole Watchlist (the single `reconcile()` entrypoint):
    uv run python -m notion_db_updater --reconcile             # one manual sweep ("run now")
    uv run python -m notion_db_updater --reconcile --limit 1   # sweep just the first entry
    uv run python -m notion_db_updater --serve                 # in-process cron loop

Verification (TASKS.md Phase 3): `--reconcile` on the real backfill transitions statuses;
re-running picks up only pending/stragglers; two concurrent triggers → the second is dropped
(single-flight); `--serve` with a short interval fires repeatedly. LangSmith shows one trace
per Entry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .app import Runtime
from .config import Settings, get_settings
from .firecrawl import FirecrawlClient
from .graph import build_graph
from .llm import extraction_model, judge_model
from .models import EXPECTED_PROPERTIES, Entry
from .notion import NotionClient
from .omdb import OMDbClient
from .schema import EnrichedEntry

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_FIXTURE_PATH = _FIXTURE_DIR / "notion_query.json"

# A query OMDb will never resolve — used to capture the 0-result fixture / branch.
_NONSENSE_TITLE = "zzqxwv no such title 1234567890"


def _print_entry(e: Entry) -> None:
    print(f"  • {e.title!r}" + (f"  [{e.media_type}]" if e.media_type else ""))
    print(f"      page_id            = {e.page_id}")
    print(f"      Enrichment Status  = {e.status}")
    print(f"      IMDB Rating        = {e.imdb_rating}")
    print(f"      RT Critic/Audience = {e.rt_critic} / {e.rt_audience}")
    print(f"      Genre              = {e.genre!r}")
    print(f"      Plot Summary       = {(e.plot or '')[:80]!r}")


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
        entries = [Entry.from_page(p) for p in results]
        print(f"matched {len(entries)} entries needing enrichment (empty OR pending):\n")
        for e in entries:
            _print_entry(e)
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


async def _generate_graph() -> None:
    """Render the graph *structure* to flow.png — no page_id, no enrichment, no live calls.

    Drawing only needs the compiled topology; `build_graph` still needs client instances to
    bind the nodes via `partial` (now incl. the Firecrawl client + extraction model for the
    RT lane), but they're never invoked here (the `async with` opens no requests).
    `draw_mermaid_png` renders through the mermaid.ink web service — the one network call this
    makes. Pass `xray=True` to `get_graph` to expand the RT subgraph's internals inline.
    """
    settings = get_settings()
    async with (
        NotionClient(settings) as notion,
        OMDbClient(settings) as omdb,
        FirecrawlClient(settings) as firecrawl,
    ):
        graph = build_graph(
            notion, omdb, firecrawl, extraction_model(settings), judge_model(settings)
        )
        # xray=True expands the RT subgraph inline (firecrawl_provider → extract) rather than
        # rendering `rt` as one opaque node — the point of the nested-lane visualization.
        graph.get_graph(xray=True).draw_mermaid_png(output_file_path="flow.png")
    print("wrote flow.png")


async def _enrich(page_id: str, capture_fixtures: bool) -> None:
    settings: Settings = get_settings()
    async with (
        NotionClient(settings) as notion,
        OMDbClient(settings) as omdb,
        FirecrawlClient(settings) as firecrawl,
    ):
        graph = build_graph(
            notion, omdb, firecrawl, extraction_model(settings), judge_model(settings)
        )
        print(f"enriching page_id = {page_id}\n")
        final = await graph.ainvoke({"page_id": page_id})

        entry: Entry | None = final.get("entry")
        enriched: EnrichedEntry | None = final.get("enriched")
        print("result:")
        print(f"  Enrichment Status  = {final.get('status')}")

        if enriched is not None:
            # A resolved Entry — print the graph's actual output contract (built by `judge`).
            print(f"  Title              = {enriched.title!r} ({enriched.year})")
            print(f"  Media type         = {enriched.media_type}")
            print(f"  IMDB id / rating   = {enriched.imdb_id} / {enriched.imdb_rating}")
            print(f"  Genre              = {enriched.genre!r}")
            print(f"  RT critic/audience = {enriched.rt_critic} / {enriched.rt_audience}")
            print(f"  RT page (title/url)= {final.get('rt_title')!r} / {final.get('rt_url')}")
            print(f"  Plot Summary       = {(enriched.plot or '')[:80]!r}")
            print(f"  Sources used       = {enriched.sources_used}")
            # Judge output — trace-only (never written to Notion; here for verification).
            print(f"  Confidence         = {enriched.confidence}")
            print(f"  Wrong match        = {final.get('wrong_match')}")
            if final.get("judge_reason"):
                print(f"  Judge reason       = {final['judge_reason']}")
        else:
            # Not resolved (blank / not-found / multi-candidate) — no contract to show.
            print(f"  Title              = {entry.title!r}" if entry else "  Title    = ?")
            if final.get("candidates") is not None:
                print(f"  OMDb candidates    = {len(final['candidates'])}")
            if final.get("note"):
                print(f"  note               = {final['note']}")

        if capture_fixtures and entry and entry.title:
            await _capture_omdb_fixtures(omdb, entry.title)


async def _reconcile(limit: int | None) -> None:
    """Run one reconcile sweep over the Watchlist ("run now"); `limit` caps it for testing."""
    async with Runtime() as rt:
        summary = await rt.reconcile(limit=limit)
    print(f"\nreconcile: {summary}")


async def _serve() -> None:
    """Run the in-process reconcile cron until interrupted."""
    async with Runtime() as rt:
        await rt.run_forever()


def _configure_logging() -> None:
    """Surface the reconcile/cron INFO logs on the console (sweep + serve modes)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enrich",
        metavar="PAGE_ID",
        help="run the OMDb enrichment graph for one Entry (Phase 2)",
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
    parser.add_argument(
        "--generate-graph",
        action="store_true",
        help="render the enrichment graph structure to flow.png (no enrichment run)",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="run one reconcile sweep over the whole Watchlist (Phase 3 'run now')",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run the in-process reconcile cron until interrupted (Phase 3)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="(with --reconcile) cap the sweep to the first N entries — a single-entry "
        "smoke test of the full sweep path",
    )
    args = parser.parse_args()

    if args.generate_graph:
        asyncio.run(_generate_graph())
    elif args.serve:
        _configure_logging()
        try:
            asyncio.run(_serve())
        except KeyboardInterrupt:
            print("\nstopped.")
    elif args.reconcile:
        _configure_logging()
        asyncio.run(_reconcile(args.limit))
    elif args.enrich:
        asyncio.run(_enrich(args.enrich, args.capture_fixtures))
    else:
        asyncio.run(_read(args.capture_fixture))


if __name__ == "__main__":
    main()

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

Phase 6d — auto-resolve stale HITL interrupts (awaiting_input past the 7-day timeout):
    uv run python -m notion_db_updater --auto-resolve-stale                  # one pass
    uv run python -m notion_db_updater --auto-resolve-stale --stale-timeout 0  # test path

Phase 9 — originate an Entry from the CLI (stands in for Slack mention `add`):
    uv run python -m notion_db_updater --add "Dune"          # create + enrich out-of-band

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
from datetime import datetime
from pathlib import Path

from .app import Runtime
from .checkpoint import open_checkpointer
from .config import Settings, get_settings
from .graph import build_graph
from .llm import (
    disambiguation_model,
    extraction_model,
    judge_model,
    llm_rate_limiter,
)
from .models import EXPECTED_PROPERTIES, Entry
from .notion import NotionClient
from .omdb import OMDbClient
from .providers import build_search_client
from .resilience import transient_retry_policy
from .schema import EnrichedEntry
from .slack import SlackTransport

log = logging.getLogger(__name__)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_FIXTURE_PATH = _FIXTURE_DIR / "notion_query.json"

# Per-run log files land here (one per invocation, named "<mode>-<timestamp>.log").
_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

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
        build_search_client(settings) as search,
    ):
        graph = build_graph(
            notion,
            omdb,
            search,
            extraction_model(settings),
            judge_model(settings),
            disambiguation_model(settings),
        )
        # xray=True expands the RT subgraph inline (rt_search → extract) rather than
        # rendering `rt` as one opaque node — the point of the nested-lane visualization.
        graph.get_graph(xray=True).draw_mermaid_png(output_file_path="flow.png")
    print("wrote flow.png")


async def _enrich(page_id: str, capture_fixtures: bool) -> None:
    settings: Settings = get_settings()
    async with (
        NotionClient(settings) as notion,
        OMDbClient(settings) as omdb,
        build_search_client(settings) as search,
        open_checkpointer(settings.CHECKPOINT_DB_PATH) as saver,
    ):
        # Checkpointed like the reconcile Runtime (thread_id = page_id), so a single-Entry run
        # writes the same checkpoint rows — the substrate Phase 6b's interrupt/resume uses.
        # Phase 7 (ADR 0013): same retry policy + shared LLM limiter as the sweep, so
        # `--enrich`/`--resume` behave identically to a reconcile run.
        limiter = llm_rate_limiter(settings)
        graph = build_graph(
            notion,
            omdb,
            search,
            extraction_model(settings, limiter),
            judge_model(settings, limiter),
            disambiguation_model(settings, limiter),
            checkpointer=saver,
            retry_policy=transient_retry_policy(settings.RETRY_MAX_ATTEMPTS),
        )
        print(f"enriching page_id = {page_id}\n")
        final_state = await graph.ainvoke(
            {"page_id": page_id}, config={"configurable": {"thread_id": page_id}}
        )

        entry: Entry | None = final_state.get("entry")
        enriched: EnrichedEntry | None = final_state.get("enriched")
        print("result:")
        print(f"  Enrichment Status  = {final_state.get('status')}")

        # Phase 6a — the disambiguation pre-filter's pick (only fires for >1 candidates).
        candidates = final_state.get("candidates") or []
        if len(candidates) > 1:
            print(f"  OMDb candidates    = {len(candidates)}")
            print(f"  Pre-filter chose   = {final_state.get('chosen_imdb_id')}")
            print(f"  Pre-filter confident = {final_state.get('confident')}")
            if final_state.get("disambiguation_reason"):
                print(f"  Pre-filter reason  = {final_state['disambiguation_reason']}")

        # Phase 6b — the run paused at interrupt() for human disambiguation. The state is
        # checkpointed under thread_id = page_id; resume out-of-band with the chosen imdbID.
        interrupts = final_state.get("__interrupt__")
        if interrupts:
            payload = interrupts[0].value
            print("  → PAUSED (awaiting_input) — pick one candidate to resume:")
            for c in payload.get("candidates", []):
                print(
                    f"      [{c['index']}] {c['title']!r} ({c['year']}) — "
                    f"{c['media_type']} — {c['imdb_id']}"
                )
            print(f"  Pre-filter best guess = {payload.get('best_guess_imdb_id')}")
            print(f"\n  resume: python -m notion_db_updater --resume {page_id} <imdbID>")
            return

        if enriched is not None:
            # A resolved Entry — print the graph's actual output contract (built by `judge`).
            print(f"  Title              = {enriched.title!r} ({enriched.year})")
            print(f"  Media type         = {enriched.media_type}")
            print(f"  IMDB id / rating   = {enriched.imdb_id} / {enriched.imdb_rating}")
            print(f"  Genre              = {enriched.genre!r}")
            print(f"  RT critic/audience = {enriched.rt_critic} / {enriched.rt_audience}")
            rt_title = final_state.get("rt_title")
            print(f"  RT page (title/url)= {rt_title!r} / {final_state.get('rt_url')}")
            print(f"  Plot (OMDb)        = {(enriched.plot or '')[:80]!r}")
            print(f"  Plot (RT)          = {(final_state.get('rt_plot') or '')[:80]!r}")
            print(f"  Sources used       = {enriched.sources_used}")
            # Judge output — trace-only (never written to Notion; here for verification).
            print(f"  Confidence         = {enriched.confidence}")
            print(f"  Wrong match        = {final_state.get('wrong_match')}")
            if final_state.get("judge_reason"):
                print(f"  Judge reason       = {final_state['judge_reason']}")
        else:
            # Not resolved (blank / not-found / multi-candidate) — no contract to show.
            print(f"  Title              = {entry.title!r}" if entry else "  Title    = ?")
            if final_state.get("candidates") is not None:
                print(f"  OMDb candidates    = {len(final_state['candidates'])}")
            if final_state.get("note"):
                print(f"  note               = {final_state['note']}")

        if capture_fixtures and entry and entry.title:
            await _capture_omdb_fixtures(omdb, entry.title)


async def _add(title: str) -> None:
    """Phase 9 (ADR 0012): originate an Entry from the CLI — stands in for Slack mention `add`.

    Dedupes, creates the page, and enriches it out-of-band exactly as the mention handler does,
    minus Slack (no completion ping / picker — a pause just prints the resume hint). Lets the
    create-then-enrich path be exercised without Slack tokens (as `--enrich`/`--resume` stand
    in for the HITL transport in earlier phases).
    """
    async with Runtime() as rt:
        existing = await rt.find_duplicate(title)
        if existing:
            print(
                f"already on the watchlist: {existing.title!r} "
                f"(status: {existing.status}) — nothing created"
            )
            return
        outcome = await rt.create_and_enrich(title)
    print(f"\ncreated {outcome.page_id} for {title!r}")
    if outcome.status == "awaiting_input":
        print("  → PAUSED (awaiting_input) — resume with the chosen imdbID:")
        print(f"  resume: python -m notion_db_updater --resume {outcome.page_id} <imdbID>")
    else:
        label = outcome.title or title
        if outcome.year:
            label += f" ({outcome.year})"
        print(f"  {label} → {outcome.status}")


async def _reconcile(limit: int | None) -> None:
    """Run one reconcile sweep over the Watchlist ("run now"); `limit` caps it for testing."""
    async with Runtime() as rt:
        summary = await rt.reconcile(limit=limit)
    print(f"\nreconcile: {summary}")


async def _auto_resolve_stale(max_age: float | None) -> None:
    """Run one Phase-6d stale-interrupt auto-resolve pass ("resolve now").

    Scans `awaiting_input` rows and auto-resolves any past the timeout with the pre-filter's
    stored best guess (confidence=low). `max_age` (from `--stale-timeout`) overrides
    `STALE_INTERRUPT_TIMEOUT_SECONDS` — pass a small value to verify the path without waiting
    7 days. Reuses the full `Runtime` wiring (clients + checkpointer + one compiled graph).
    """
    async with Runtime() as rt:
        summary = await rt.auto_resolve_stale(max_age=max_age)
    print(f"\nstale-interrupt: {summary}")


async def _resume(page_id: str, chosen_imdb_id: str) -> None:
    """Resume a paused HITL run (Phase 6b) with the human's chosen imdbID — the test harness.

    Stands in for the Phase-6c Slack action handler: drives `Runtime.resume` (out-of-band, no
    single-flight lock) on the same thread_id = page_id, so the paused `await_human` node gets
    the pick and the graph runs to completion. Reuses the full `Runtime` wiring (clients +
    checkpointer + one compiled graph).
    """
    async with Runtime() as rt:
        result = await rt.resume(page_id, chosen_imdb_id)
    label = result.title or chosen_imdb_id
    if result.year:
        label += f" ({result.year})"
    print(f"resumed {page_id}: {label} [{chosen_imdb_id}] → {result.status}")


async def _serve(limit: int | None = None) -> None:
    """Run the reconcile cron + (when Slack is configured) the Socket Mode listener.

    Slack is the app's inbound path (ADR 0009 / 0010): it posts the HITL picker when a run
    pauses and handles the `@movie-bot add <title>` and `@movie-bot run` commands. The picker
    notifier is wired to the same `Runtime` the cron drives, so a paused sweep prompts in Slack
    and the button click resumes it. When Slack tokens are unset (local dev), falls back to
    cron-only.

    `limit` caps each sweep to the first N pending entries (testing aid): with the 1-hour
    interval, `--serve --limit 1` processes one ambiguous Entry → one Slack picker, then idles
    with the listener open so the click round-trip can be verified in isolation.
    """
    settings = get_settings()
    async with Runtime(settings) as rt:
        tasks = [rt.run_forever(limit=limit)]
        if settings.SLACK_BOT_TOKEN and settings.SLACK_APP_TOKEN:
            slack = SlackTransport(settings, rt)
            rt.set_notifier(slack.post_picker)
            rt.bind_completion_notifier(slack.post_completion)  # Phase 9 add completion ping
            tasks.append(slack.start())
            log.info("serve: Slack Socket Mode enabled (HITL picker + @mention add/run)")
        else:
            log.info("serve: Slack tokens unset — cron only (no HITL picker)")
        await asyncio.gather(*tasks)


def _configure_logging(mode: str) -> None:
    """Tee the INFO logs to the console *and* a fresh per-run file under ``logs/``.

    Each invocation gets its own file, ``logs/<mode>-<timestamp>.log`` (e.g.
    ``logs/reconcile-20260703-153012.log``), so a run's logs are isolated and easy to find by
    what it did. The module loggers (``logging.getLogger(__name__)`` in app/graph/rt/slack)
    all flow into both handlers — no call-site changes needed. Files are never pruned (add
    ``logs/`` to .gitignore); prune manually if they pile up.
    """
    _LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = _LOG_DIR / f"{mode}-{stamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log.info("logging to %s", log_path)


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
        "--resume",
        nargs=2,
        metavar=("PAGE_ID", "IMDB_ID"),
        help="resume a paused HITL run (Phase 6b) with the chosen imdbID — the manual picker",
    )
    parser.add_argument(
        "--add",
        metavar="TITLE",
        help=(
            "originate an Entry from the CLI and enrich it (Phase 9 — stands in for Slack add)"
        ),
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
        "--auto-resolve-stale",
        action="store_true",
        help="run one Phase-6d stale-interrupt auto-resolve pass over awaiting_input rows",
    )
    parser.add_argument(
        "--stale-timeout",
        type=float,
        metavar="SECONDS",
        help="(with --auto-resolve-stale) override the 7-day timeout — small value to test",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="(with --reconcile/--serve) cap each sweep to the first N pending entries — a "
        "single-entry smoke test (with --serve: one picker, then idle)",
    )
    args = parser.parse_args()

    if args.generate_graph:
        _configure_logging("graph")
        asyncio.run(_generate_graph())
    elif args.serve:
        _configure_logging("serve")
        try:
            asyncio.run(_serve(args.limit))
        except KeyboardInterrupt:
            print("\nstopped.")
    elif args.add:
        _configure_logging("add")
        asyncio.run(_add(args.add))
    elif args.reconcile:
        _configure_logging("reconcile")
        asyncio.run(_reconcile(args.limit))
    elif args.auto_resolve_stale:
        _configure_logging("stale")
        asyncio.run(_auto_resolve_stale(args.stale_timeout))
    elif args.resume:
        _configure_logging("resume")
        asyncio.run(_resume(args.resume[0], args.resume[1]))
    elif args.enrich:
        _configure_logging("enrich")
        asyncio.run(_enrich(args.enrich, args.capture_fixtures))
    else:
        _configure_logging("read")
        asyncio.run(_read(args.capture_fixture))


if __name__ == "__main__":
    main()

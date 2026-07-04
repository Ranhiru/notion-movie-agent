"""The reconcile sweep and its in-process cron — Phase 3 (ADR 0001 / 0004).

`reconcile()` is the **single entrypoint** that sweeps the Watchlist: it queries every Entry
still needing enrichment (Status empty OR `pending`) and runs each through the Phase-2
enrichment graph, bounded by a semaphore so an N-row backfill can't fan out into an API
429-storm. A process-wide `asyncio.Lock` enforces **single-flight**: while one reconcile is
running, an extra trigger is logged and dropped (ADR 0001's ack/log/drop), and the hourly
cron picks up any stragglers on its next pass.

`Runtime` owns the long-lived state — the two API clients, the one compiled graph, and the
lock — as an async context manager. It is deliberately the home that Phase 6 extends with the
`AsyncSqliteSaver` checkpointer, the Slack app, and the out-of-band in-flight set.

Status lifecycle (ADR 0004) is split between the graph and this sweep:
  - The graph nodes write the **definitive** outcomes — `done` (resolved) and `failed` (blank
    Entry or a real OMDb not-found).
  - An **ambiguous** multi-candidate the pre-filter isn't sure about pauses the graph at
    `interrupt()`; this sweep marks it `awaiting_input`, posts the HITL picker (Phase 6c), and
    moves on — an out-of-band resume finishes it (ADR 0006).
  - A **transient** error (network / 5xx / exhausted 429) raises out of the graph before its
    terminal write, so nothing is written and the Entry keeps its `pending`/unset status for
    the next cron pass. `reconcile()` catches it here and never writes `failed` — `failed` is
    reserved for definitive core failures only, so cron self-healing is preserved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from .checkpoint import open_checkpointer
from .config import Settings, get_settings
from .firecrawl import FirecrawlClient
from .graph import build_graph
from .llm import disambiguation_model, extraction_model, judge_model
from .models import Entry, enrichment_properties
from .notion import NotionClient
from .omdb import OMDbClient

log = logging.getLogger(__name__)

# Returned by _run_one when the graph raised a transient error: the Entry was left in its
# prior (pending/unset) state — distinct from a graph-written terminal status.
_TRANSIENT = "error"
# Returned by _run_one when the graph paused at an interrupt() (Phase 6b): the run is
# checkpointed under thread_id = page_id and the Entry marked `awaiting_input`, awaiting an
# out-of-band resume. Distinct from a terminal status — the sweep just moves on (ADR 0006).
_AWAITING = "awaiting_input"


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    """Outcome tally for one reconcile pass (logged + returned for verification)."""

    dropped: bool = False  # a reconcile was already running → this trigger was dropped
    total: int = 0
    done: int = 0
    failed: int = 0
    # graph-written "pending" — not produced since 6a (multi-candidate auto-resolves via the
    # pre-filter, or escalates to `awaiting_input` in 6b); kept for the tally.
    pending: int = 0
    # Phase 6b: paused at interrupt() → marked awaiting_input, awaiting an out-of-band resume.
    awaiting_input: int = 0
    error: int = 0  # transient failure → Entry left pending for the next cron pass

    @classmethod
    def from_statuses(cls, statuses: list[str]) -> ReconcileSummary:
        c = Counter(statuses)
        return cls(
            total=len(statuses),
            done=c["done"],
            failed=c["failed"],
            pending=c["pending"],
            awaiting_input=c[_AWAITING],
            error=c[_TRANSIENT],
        )

    def __str__(self) -> str:
        if self.dropped:
            return "dropped (a reconcile is already in progress)"
        return (
            f"{self.total} entries → {self.done} done, {self.failed} failed, "
            f"{self.pending} pending, {self.awaiting_input} awaiting input, "
            f"{self.error} transient error(s) left pending"
        )


@dataclass(frozen=True, slots=True)
class ResumeResult:
    """Outcome of a HITL resume — enough for the Slack handler to show a human-friendly result.

    `status` is the terminal graph status (`done` / `failed`); `title` / `year` / `imdb_id` are
    the *resolved* identity (from the completed run's `EnrichedEntry`), so the picker message
    can render "✅ *The Agency* (2024) — IMDb" instead of a bare imdbID.
    """

    status: str
    title: str | None = None
    year: int | None = None
    imdb_id: str | None = None

    @classmethod
    def from_state(cls, values: dict) -> ResumeResult:
        enriched = values.get("enriched")  # EnrichedEntry | None — present once status == done
        return cls(
            status=values.get("status", "done"),
            title=(enriched.title if enriched else None) or values.get("omdb_title"),
            year=(enriched.year if enriched else None) or values.get("year"),
            imdb_id=values.get("imdb_id") or values.get("chosen_imdb_id"),
        )


class Runtime:
    """Owns the long-lived clients, graph, and single-flight lock for the reconcile sweep.

    Use as an async context manager so both HTTP clients are closed::

        async with Runtime() as rt:
            await rt.reconcile()        # one manual sweep
            await rt.run_forever()      # or the in-process cron loop
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._notion = NotionClient(self._settings)
        self._omdb = OMDbClient(self._settings)
        self._firecrawl = FirecrawlClient(self._settings)
        self._concurrency = self._settings.RECONCILE_CONCURRENCY
        # Single-flight: only one reconcile runs at a time (ADR 0001).
        self._lock = asyncio.Lock()
        # The compiled graph is built in __aenter__: it needs the AsyncSqliteSaver, whose
        # setup() is async (ADR 0006 / 0007). Held via an AsyncExitStack so the checkpointer's
        # connection is closed on exit.
        self._stack = contextlib.AsyncExitStack()
        self._graph: CompiledStateGraph | None = None
        # Optional interrupt notifier (Phase 6c): set to `SlackTransport.post_picker` so a
        # paused run posts its candidate picker to Slack. Left None (no notification) for CLI.
        self._notifier: Callable[[str, dict], Awaitable[None]] | None = None

    def set_notifier(self, notifier: Callable[[str, dict], Awaitable[None]]) -> None:
        """Register the interrupt notifier (`page_id`, payload) → posts the HITL picker.

        Wired by `_serve` to `SlackTransport.post_picker` (ADR 0010). Kept separate from
        `__init__` because the Slack transport needs a live `Runtime` to bind its handlers, so
        the two are wired after the Runtime is entered.
        """
        self._notifier = notifier

    async def __aenter__(self) -> Runtime:
        # Open the durable checkpointer (thread_id = page_id) and compile the one graph reused
        # across entries. `open_checkpointer` applies the state-type allowlist + runs setup()
        # (WAL lifecycle proven by spike 03). Models are bound once (ChatOpenAI is stateless
        # per .invoke); the extraction model is shared by the RT lane.
        saver = await self._stack.enter_async_context(
            open_checkpointer(self._settings.CHECKPOINT_DB_PATH)
        )
        self._graph = build_graph(
            self._notion,
            self._omdb,
            self._firecrawl,
            extraction_model(self._settings),
            judge_model(self._settings),
            disambiguation_model(self._settings),
            checkpointer=saver,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._stack.aclose()  # closes the checkpointer's sqlite connection
        await self._notion.aclose()
        await self._omdb.aclose()
        await self._firecrawl.aclose()

    async def reconcile(self, limit: int | None = None) -> ReconcileSummary:
        """Run one reconcile sweep, unless one is already in progress (then drop).

        `limit` caps the sweep to the first N queried entries — a testing aid for smoke-testing
        the full sweep path (query → lock → semaphore → graph → tally) against one or a few
        entries without burning the whole backlog. `None` (the default, used by the cron)
        processes every pending entry.

        The `locked()` check and `async with` acquire run with no `await` between them, so in
        single-threaded asyncio they are atomic — no lost-wakeup race on the drop path.
        """
        if self._lock.locked():
            log.info("reconcile already in progress — dropping this trigger")
            return ReconcileSummary(dropped=True)
        async with self._lock:
            return await self._sweep(limit)

    async def _sweep(self, limit: int | None = None) -> ReconcileSummary:
        entries = await self._notion.query_entries()
        if limit is not None:
            log.info("reconcile: %d entries match; limiting to first %d", len(entries), limit)
            entries = entries[:limit]
        log.info("reconcile: %d entries to enrich", len(entries))
        sem = asyncio.Semaphore(self._concurrency)
        statuses = await asyncio.gather(*(self._run_one(e, sem) for e in entries))
        summary = ReconcileSummary.from_statuses(statuses)
        log.info("reconcile complete: %s", summary)
        return summary

    async def _run_one(self, entry: Entry, sem: asyncio.Semaphore) -> str:
        """Enrich one Entry through the graph; classify pause/transient outcomes for the tally.

        Returns the graph's terminal status (`done` / `failed`), `_AWAITING` if the run paused
        at an interrupt() for human disambiguation (Phase 6b — the Entry is marked
        `awaiting_input` and the sweep moves on, ADR 0006), or `_TRANSIENT` if the run raised —
        in which case nothing was written and the Entry stays pending for the next cron pass
        (ADR 0004). `failed` is only ever written by the graph, on a definitive not-found.
        """
        assert self._graph is not None, "graph not compiled — use `async with Runtime()`"
        async with sem:
            try:
                final_state = await self._graph.ainvoke(
                    {"page_id": entry.page_id},
                    # thread_id = page_id keys the checkpoint (ADR 0006/0007), so a Phase-6b
                    # paused run resumes on this same key. run_name names the LangSmith trace;
                    # `origin` foreshadows the Phase 9 Slack `/add` path (ADR 0001).
                    config={
                        "configurable": {"thread_id": entry.page_id},
                        "run_name": entry.title or "(blank Entry)",
                        "metadata": {"page_id": entry.page_id, "origin": "sweep"},
                    },
                )
                interrupts = final_state.get("__interrupt__")
                if interrupts:
                    # Paused for human disambiguation (Phase 6b). The run is checkpointed
                    # under thread_id = page_id; mark the Entry awaiting_input so the next
                    # sweep skips it (the filter queries empty/pending only) and an
                    # out-of-band resume finishes it. `update_notion` never ran → write here.
                    await self._notion.update_entry(
                        entry.page_id, enrichment_properties(status=_AWAITING)
                    )
                    # Post the HITL picker (Phase 6c), if a notifier is wired. Best-effort: a
                    # Slack failure must not fail the sweep — the row stays awaiting_input and
                    # the next `@movie-bot run` (or manual --enrich) can re-prompt.
                    if self._notifier is not None:
                        try:
                            await self._notifier(entry.page_id, interrupts[0].value)
                        except Exception:
                            log.exception("reconcile: failed to notify for %s", entry.page_id)
                    log.info(
                        "reconcile: %r (%s) awaiting human input", entry.title, entry.page_id
                    )
                    return _AWAITING
                return final_state.get("status", "pending")
            except Exception:
                log.exception(
                    "reconcile: transient error on %r (%s) — left pending",
                    entry.title,
                    entry.page_id,
                )
                return _TRANSIENT

    async def resume(self, page_id: str, chosen_imdb_id: str) -> ResumeResult:
        """Resume a paused HITL run with the human's chosen imdbID (ADR 0006 — out-of-band).

        Called from *outside* the sweep — a test harness in Phase 6b, the Slack action handler
        in 6c. Does **not** take the single-flight lock: the lock serializes sweep-vs-sweep,
        while sweep-vs-resume is partitioned by status (the sweep queries pending; this row is
        `awaiting_input`, so the two paths can never touch the same row). Feeds the pick into
        the paused `await_human` node via `Command(resume=...)` on the same `thread_id =
        page_id`; the graph runs on through `omdb_details` → … → `update_notion`, which writes
        the terminal status. Returns a `ResumeResult` (status + resolved identity).

        A resume on an already-finished thread is a **safe no-op** (ADR 0006): if the
        checkpoint has nothing pending (`state.next == ()`) — e.g. a Slack double-click, or two
        people clicking — we return the stored result without re-invoking, so the graph never
        re-runs `update_notion` or double-writes Notion.
        """
        assert self._graph is not None, "graph not compiled — use `async with Runtime()`"
        state = await self._graph.aget_state({"configurable": {"thread_id": page_id}})
        if not state.next:
            log.info("resume: %s already resolved — no-op (double click?)", page_id)
            return ResumeResult.from_state(state.values)
        final_state = await self._graph.ainvoke(
            Command(resume=chosen_imdb_id),
            config={
                "configurable": {"thread_id": page_id},
                "run_name": f"resume {page_id}",
                "metadata": {"page_id": page_id, "origin": "resume"},
            },
        )
        return ResumeResult.from_state(final_state)

    async def run_forever(
        self, interval: float | None = None, limit: int | None = None
    ) -> None:
        """In-process cron: run reconcile, sleep, repeat (ADR 0001's hourly safety net).

        `interval` defaults to `RECONCILE_INTERVAL_SECONDS` (shorten via env to test the
        scheduler). A failed cycle is logged and the loop continues — one bad pass must not
        kill the long-lived process (ADR 0009: this *is* the always-on process before Docker).

        `limit` caps each sweep to the first N pending entries — a testing aid for `--serve`:
        with the default 1-hour interval, a bounded startup sweep processes just those N rows
        (e.g. one ambiguous Entry → one Slack picker) and then idles, keeping the Socket Mode
        listener alive so the button click can be received.
        """
        period = (
            interval if interval is not None else self._settings.RECONCILE_INTERVAL_SECONDS
        )
        log.info("starting reconcile cron (every %gs) — Ctrl-C to stop", period)
        while True:
            try:
                await self.reconcile(limit=limit)
            except Exception:
                log.exception("reconcile cron cycle failed — continuing")
            await asyncio.sleep(period)

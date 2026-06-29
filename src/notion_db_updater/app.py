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
  - The graph nodes write the **definitive** outcomes — `done` (resolved), `failed` (blank
    Entry or a real OMDb not-found), and `pending`+note (multi-candidate, deferred to 6a).
  - A **transient** error (network / 5xx / exhausted 429) raises out of the graph before its
    terminal write, so nothing is written and the Entry keeps its `pending`/unset status for
    the next cron pass. `reconcile()` catches it here and never writes `failed` — `failed` is
    reserved for definitive core failures only, so cron self-healing is preserved.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass

from .config import Settings, get_settings
from .graph import build_graph
from .models import Entry
from .notion import NotionClient
from .omdb import OMDbClient

log = logging.getLogger(__name__)

# Returned by _run_one when the graph raised a transient error: the Entry was left in its
# prior (pending/unset) state — distinct from the graph-written "pending" (multi-candidate).
_TRANSIENT = "error"


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    """Outcome tally for one reconcile pass (logged + returned for verification)."""

    dropped: bool = False  # a reconcile was already running → this trigger was dropped
    total: int = 0
    done: int = 0
    failed: int = 0
    pending: int = 0  # graph-written: multi-candidate, deferred to Phase 6a
    error: int = 0  # transient failure → Entry left pending for the next cron pass

    @classmethod
    def from_statuses(cls, statuses: list[str]) -> ReconcileSummary:
        c = Counter(statuses)
        return cls(
            total=len(statuses),
            done=c["done"],
            failed=c["failed"],
            pending=c["pending"],
            error=c[_TRANSIENT],
        )

    def __str__(self) -> str:
        if self.dropped:
            return "dropped (a reconcile is already in progress)"
        return (
            f"{self.total} entries → {self.done} done, {self.failed} failed, "
            f"{self.pending} pending (multi-candidate), "
            f"{self.error} transient error(s) left pending"
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
        # One compiled graph, reused across entries — it is stateless without a checkpointer
        # (the checkpointer arrives in Phase 6a, keyed thread_id = page_id).
        self._graph = build_graph(self._notion, self._omdb)
        self._concurrency = self._settings.RECONCILE_CONCURRENCY
        # Single-flight: only one reconcile runs at a time (ADR 0001).
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._notion.aclose()
        await self._omdb.aclose()

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
        """Enrich one Entry through the graph; classify a transient failure as left-pending.

        Returns the graph's terminal status (`done` / `failed` / `pending`), or `_TRANSIENT`
        if the run raised — in which case nothing was written and the Entry stays pending for
        the next cron pass (ADR 0004). `failed` is only ever written by the graph, on a
        definitive not-found.
        """
        async with sem:
            try:
                final = await self._graph.ainvoke(
                    {"page_id": entry.page_id},
                    # Names the LangSmith trace by the entry's title and tags the sweep origin
                    # (ADR 0001; `origin` foreshadows the Phase 9 Slack `/add` path).
                    config={
                        "run_name": entry.title or "(blank Entry)",
                        "metadata": {"page_id": entry.page_id, "origin": "sweep"},
                    },
                )
                return final.get("status", "pending")
            except Exception:
                log.exception(
                    "reconcile: transient error on %r (%s) — left pending",
                    entry.title,
                    entry.page_id,
                )
                return _TRANSIENT

    async def run_forever(self, interval: float | None = None) -> None:
        """In-process cron: run reconcile, sleep, repeat (ADR 0001's hourly safety net).

        `interval` defaults to `RECONCILE_INTERVAL_SECONDS` (shorten via env to test the
        scheduler). A failed cycle is logged and the loop continues — one bad pass must not
        kill the long-lived process (ADR 0009: this *is* the always-on process before Docker).
        """
        period = (
            interval if interval is not None else self._settings.RECONCILE_INTERVAL_SECONDS
        )
        log.info("starting reconcile cron (every %gs) — Ctrl-C to stop", period)
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile cron cycle failed — continuing")
            await asyncio.sleep(period)

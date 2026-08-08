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
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from .checkpoint import open_checkpointer
from .config import Settings, get_settings
from .graph import NOT_FOUND, SLACK_ORIGIN, CompletionNotifier, build_graph
from .llm import (
    disambiguation_model,
    extraction_model,
    judge_model,
    llm_rate_limiter,
)
from .models import Entry, enrichment_properties
from .notion import NotionClient
from .omdb import OMDbClient
from .providers import build_search_client
from .resilience import transient_retry_policy

log = logging.getLogger(__name__)

# Friendly Slack progress emitted after real graph nodes complete. The graph's OMDb and RT
# lanes run concurrently, so these describe durable milestones rather than pretending to be a
# percentage-complete counter. Nodes omitted here either finish almost immediately or have
# their own UI (await_human posts the candidate picker; notify renders the final result).
_PROGRESS_AFTER_NODE = {
    "read_page": "🔎 Searching movie databases…",
    "omdb_search": "🎬 Checking the best title match…",
    "disambiguate": "🧭 Resolving the title identity…",
    "omdb_details": "📚 Movie details found — combining sources…",
    "rt": "🍅 Rotten Tomatoes lookup finished — combining sources…",
    "assemble": "🧩 Comparing source results…",
    "resolve_rt": "✅ Verifying the match…",
    "judge": "📝 Updating Notion…",
}

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


@dataclass(frozen=True, slots=True)
class AddOutcome:
    """Result of a Slack mention `add` create-then-enrich (Phase 9 / ADR 0012).

    `status` is the terminal graph status (`done` / `failed`) or `awaiting_input` when the run
    paused for human disambiguation — enough for the `--add` CLI / mention handler to report
    what happened to the freshly-created `page_id`. The completion *ping* itself is posted by
    the graph's `notify` node, not from here (a paused run resolves much later).
    """

    page_id: str
    status: str
    title: str | None = None
    year: int | None = None


@dataclass(frozen=True, slots=True)
class StaleSummary:
    """Outcome tally for one stale-interrupt auto-resolve pass (Phase 6d)."""

    total: int = 0  # rows in awaiting_input at the start of the pass
    resolved: int = 0  # aged past the timeout → auto-resolved with the best guess
    fresh: int = 0  # still within the timeout → left for the human
    no_guess: int = 0  # aged out, had candidates but no best guess (LLM fail-safe) → left
    not_found: int = 0  # aged out, 0-candidate not-found escalation (6f) → resolved `failed`
    already_done: int = 0  # checkpoint had nothing pending (Notion status lagged) → skipped

    def __str__(self) -> str:
        return (
            f"{self.total} awaiting_input → {self.resolved} auto-resolved, "
            f"{self.fresh} still fresh, {self.no_guess} no best-guess (left), "
            f"{self.not_found} not-found → failed, {self.already_done} already resolved"
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
        # Phase 9 (ADR 0012): the terminal `notify` node's late-bound Slack poster (bound by
        # `_serve` once the transport exists) and the process-global in-flight guard — page_ids
        # currently being enriched out-of-band by a Slack `add`, which the sweep must skip so a
        # concurrent cron doesn't double-enrich the same still-`pending` row.
        self._completion_notifier = CompletionNotifier()
        self._inflight: set[str] = set()

    def set_notifier(self, notifier: Callable[[str, dict], Awaitable[None]]) -> None:
        """Register the interrupt notifier (`page_id`, payload) → posts the HITL picker.

        Wired by `_serve` to `SlackTransport.post_picker` (ADR 0010). Kept separate from
        `__init__` because the Slack transport needs a live `Runtime` to bind its handlers, so
        the two are wired after the Runtime is entered.
        """
        self._notifier = notifier

    def bind_completion_notifier(
        self, post: Callable[[str, str, str | None], Awaitable[None]]
    ) -> None:
        """Bind the Slack completion poster the terminal `notify` node uses (Phase 9).

        Wired by `_serve` to `SlackTransport.post_completion` after the transport is built (the
        graph — and its `notify` node's holder — is already compiled by then). Until bound, the
        node no-ops, so a Slack-less process or a `--add` CLI run simply skips the ping.
        """
        self._completion_notifier.bind(post)

    async def __aenter__(self) -> Runtime:
        # Open the durable checkpointer (thread_id = page_id) and compile the one graph reused
        # across entries. `open_checkpointer` applies the state-type allowlist + runs setup()
        # (WAL lifecycle proven by spike 03). Models are bound once (ChatOpenAI is stateless
        # per .invoke); the extraction model is shared by the RT lane.
        saver = await self._stack.enter_async_context(
            open_checkpointer(self._settings.CHECKPOINT_DB_PATH)
        )
        # Phase 8 (ADR 0003): the RT search strategy — a bare provider or the round-robin
        # composite — built from SEARCH_PROVIDERS; the exit stack closes its clients on aclose.
        search = await self._stack.enter_async_context(build_search_client(self._settings))
        # Phase 7 (ADR 0013): one shared LLM rate limiter for all three role models (aggregate
        # cap on the single endpoint), and the transient-retry policy for the gating nodes.
        limiter = llm_rate_limiter(self._settings)
        self._graph = build_graph(
            self._notion,
            self._omdb,
            search,
            extraction_model(self._settings, limiter),
            judge_model(self._settings, limiter),
            disambiguation_model(self._settings, limiter),
            checkpointer=saver,
            retry_policy=transient_retry_policy(self._settings.RETRY_MAX_ATTEMPTS),
            completion_notifier=self._completion_notifier,  # Phase 9 terminal notify node
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        # The exit stack closes the checkpointer's sqlite connection *and* the RT search
        # clients (opened via build_search_client in __aenter__).
        await self._stack.aclose()
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
        # Phase 9 (ADR 0012): skip any row a Slack `add` is enriching out-of-band right
        # now — it's still `pending` (its terminal write hasn't landed), so it matches the
        # sweep filter, but re-enriching it would double external-API spend (the hazard ADR
        # 0001 prevents).
        # Crash-safe: on a crash the set vanishes and the row is still `pending`, so the next
        # cron reclaims it (ADR 0004 self-heal — no `enriching` Notion status needed).
        if self._inflight:
            before = len(entries)
            entries = [e for e in entries if e.page_id not in self._inflight]
            skipped = before - len(entries)
            if skipped:
                log.info("reconcile: skipping %d entry(ies) enriching out-of-band", skipped)
        if limit is not None:
            log.info("reconcile: %d entries match; limiting to first %d", len(entries), limit)
            entries = entries[:limit]
        log.info("reconcile: %d entries to enrich", len(entries))
        sem = asyncio.Semaphore(self._concurrency)
        # `return_exceptions=True` so one Entry's unexpected escape (or a CancelledError) can't
        # cancel the whole gather and abort its siblings (ADR 0013 — batch isolation). _run_one
        # already catches `Exception`; this backstops anything that slips past it. Per-Entry
        # state is independently checkpointed (thread_id = page_id), so this only guards the
        # *scheduling* of the batch, never a shared rollback.
        results = await asyncio.gather(
            *(self._run_one(e, sem) for e in entries), return_exceptions=True
        )
        statuses = [self._classify_result(e, r) for e, r in zip(entries, results, strict=True)]
        summary = ReconcileSummary.from_statuses(statuses)
        log.info("reconcile complete: %s", summary)
        return summary

    def _classify_result(self, entry: Entry, result: str | BaseException) -> str:
        """Map a `gather(return_exceptions=True)` result to a tally status (ADR 0013).

        A returned string is `_run_one`'s own classification (`done`/`failed`/`_AWAITING`/…). A
        returned exception means something escaped `_run_one`'s own `except` — treat it like a
        transient error (Entry left untouched → stays pending for the cron) so one bad Entry
        doesn't sink the pass.
        """
        if isinstance(result, BaseException):
            log.error(
                "reconcile: unhandled error escaped _run_one for %r (%s) — counted transient",
                entry.title,
                entry.page_id,
                exc_info=result,
            )
            return _TRANSIENT
        return result

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
                config: RunnableConfig = {
                    # thread_id = page_id keys the checkpoint (ADR 0006/0007), so a Phase-6b
                    # paused run resumes on this same key. run_name names the LangSmith trace;
                    # `origin` foreshadows the Phase 9 Slack `add` path (ADR 0001).
                    "configurable": {"thread_id": entry.page_id},
                    "run_name": entry.title or "(blank Entry)",
                    "metadata": {"page_id": entry.page_id, "origin": "sweep"},
                }
                # Phase 7 (ADR 0013): cap intra-graph parallel-node execution (fan-out lanes)
                # on top of the across-Entry sweep semaphore. 0 = unset → let LangGraph decide.
                if self._settings.GRAPH_MAX_CONCURRENCY > 0:
                    config["max_concurrency"] = self._settings.GRAPH_MAX_CONCURRENCY
                final_state = await self._graph.ainvoke(
                    {"page_id": entry.page_id}, config=config
                )
                if await self._handle_pause(entry, final_state):
                    return _AWAITING
                return final_state.get("status", "pending")
            except Exception:
                log.exception(
                    "reconcile: transient error on %r (%s) — left pending",
                    entry.title,
                    entry.page_id,
                )
                return _TRANSIENT

    async def _handle_pause(self, entry: Entry, final_state: dict) -> bool:
        """If a run paused at `interrupt()`: mark awaiting_input + post the picker. → paused?

        Shared by the reconcile sweep (`_run_one`) and the Phase-9 out-of-band Slack `add`
        (`create_and_enrich`), so an add that turns ambiguous pauses and prompts in Slack
        exactly like a swept row (ADR 0006 / 0012). The run is checkpointed under
        `thread_id = page_id`; `update_notion` never ran, so we write `awaiting_input` here so
        the next sweep skips it (the filter queries empty/pending only) and an out-of-band
        resume finishes it. Posting the picker is best-effort — a Slack failure must not fail
        the caller; the row stays awaiting_input for the next `@movie-bot run` / `--enrich`.
        Returns True when it handled a pause, False for a terminal run.
        """
        interrupts = final_state.get("__interrupt__")
        if not interrupts:
            return False
        await self._notion.update_entry(entry.page_id, enrichment_properties(status=_AWAITING))
        if self._notifier is not None:
            try:
                await self._notifier(entry.page_id, interrupts[0].value)
            except Exception:
                log.exception("failed to post HITL picker for %s", entry.page_id)
        log.info("%r (%s) awaiting human input", entry.title, entry.page_id)
        return True

    async def find_duplicate(self, title: str) -> Entry | None:
        """Best-effort pre-create dedupe for Slack `add` (Phase 9): an Entry, or None."""
        return await self._notion.find_by_title(title)

    async def create_and_enrich(
        self,
        title: str,
        *,
        channel: str | None = None,
        user: str | None = None,
        thread_ts: str | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> AddOutcome:
        """Create a Watchlist page from Slack `add` and enrich it out-of-band.

        Phase 9 / ADR 0012. Creates the page (Entry only, `pending`), then runs the *same*
        enrichment graph on its `page_id` — but **without** the single-flight lock (the lock
        serializes sweep-vs-sweep; this is an interactive, one-page run). Instead the `page_id`
        is held in the in-flight guard for the whole run so a concurrent cron sweep skips it
        (`_sweep`), closing the double-enrich hazard ADR 0012 calls out. `origin=slack` + the
        notify context ride in on the initial state (checkpointed → durable across the
        interrupt/resume), so the terminal `notify` node posts the completion ping — even if
        the run only resolves days later.

        Returns an `AddOutcome`; a run that turns ambiguous pauses at `interrupt()`
        (`awaiting_input`) and posts the HITL picker via `_handle_pause`, resolved later by a
        Slack click / the 6d timeout — same as a swept row.
        """
        assert self._graph is not None, "graph not compiled — use `async with Runtime()`"
        entry = await self._notion.create_entry(title)
        page_id = entry.page_id
        log.info("add: created page %s for %r — enriching out-of-band", page_id, title)
        await self._report_progress(progress, "✅ Notion entry created — starting enrichment…")
        # Hold the page_id in-flight until its terminal/awaiting_input write lands (inside
        # _handle_pause or update_notion), so the sweep never sees it while still `pending`.
        self._inflight.add(page_id)
        try:
            config: RunnableConfig = {
                "configurable": {"thread_id": page_id},
                "run_name": title,
                "metadata": {"page_id": page_id, "origin": SLACK_ORIGIN},
            }
            if self._settings.GRAPH_MAX_CONCURRENCY > 0:
                config["max_concurrency"] = self._settings.GRAPH_MAX_CONCURRENCY
            initial_state = {
                "page_id": page_id,
                "origin": SLACK_ORIGIN,
                "notify_channel": channel,
                "notify_user": user,
                "notify_thread_ts": thread_ts,
            }
            # Stream state updates so Slack reflects nodes that have actually completed.
            # The checkpointer remains authoritative for the final state and durable resume.
            interrupts = None
            async for update in self._graph.astream(
                initial_state, config=config, stream_mode="updates"
            ):
                if "__interrupt__" in update:
                    interrupts = update["__interrupt__"]
                for node_name in update:
                    message = _PROGRESS_AFTER_NODE.get(node_name)
                    if message is not None:
                        await self._report_progress(progress, message)

            snapshot = await self._graph.aget_state(config)
            final_state = dict(snapshot.values)
            if interrupts:
                final_state["__interrupt__"] = interrupts
            if await self._handle_pause(entry, final_state):
                await self._report_progress(
                    progress, "🧭 I need your help choosing the correct title."
                )
                return AddOutcome(page_id=page_id, status=_AWAITING, title=title)
            result = ResumeResult.from_state(final_state)
            return AddOutcome(
                page_id=page_id,
                status=result.status,
                title=result.title or title,
                year=result.year,
            )
        finally:
            self._inflight.discard(page_id)

    @staticmethod
    async def _report_progress(
        progress: Callable[[str], Awaitable[None]] | None, message: str
    ) -> None:
        """Send best-effort interactive progress without making Slack gate enrichment."""
        if progress is None:
            return
        try:
            await progress(message)
        except Exception:  # noqa: BLE001 — progress is UX; the durable graph must keep running
            log.exception("add: failed to update Slack progress message")

    async def resume(
        self, page_id: str, chosen_imdb_id: str, *, auto_resolved: bool = False
    ) -> ResumeResult:
        """Resume a paused HITL run with the chosen imdbID (ADR 0006 — out-of-band).

        Called from *outside* the sweep — a test harness in Phase 6b, the Slack action handler
        in 6c, the 7-day stale-interrupt pass in 6d. Does **not** take the single-flight lock:
        the lock serializes sweep-vs-sweep, while sweep-vs-resume is partitioned by status (the
        sweep queries pending; this row is `awaiting_input`, so the two paths can never touch
        the same row). Feeds the pick into the paused `await_human` node via
        `Command(resume=...)` on the same `thread_id = page_id`; the graph runs on through
        `omdb_details` → … → `update_notion`, which writes the terminal status. Returns a
        `ResumeResult` (status + resolved identity).

        `auto_resolved=True` marks this as the Phase-6d timeout auto-resolve (`chosen_imdb_id`
        is the pre-filter's stored best guess, no human confirmed it): we flag the graph state
        so `judge` grades it `confidence=low` (trace-only, flagged for review) instead of
        running the LLM. `origin=auto_resolve` in the trace metadata distinguishes it from a
        human resume.

        A resume on an already-finished thread is a **safe no-op** (ADR 0006): if the
        checkpoint has nothing pending (`state.next == ()`) — e.g. a Slack double-click, or two
        people clicking — we return the stored result without re-invoking, so the graph never
        re-runs `update_notion` or double-writes Notion.
        """
        assert self._graph is not None, "graph not compiled — use `async with Runtime()`"
        config: RunnableConfig = {"configurable": {"thread_id": page_id}}
        state = await self._graph.aget_state(config)
        if not state.next:
            log.info("resume: %s already resolved — no-op (double click?)", page_id)
            return ResumeResult.from_state(state.values)
        # For the 6d timeout path, flag the state *as part of the resume* (`Command(update=…)`)
        # so `judge` short-circuits to confidence=low. A standalone `aupdate_state` on a graph
        # paused at the fan-in interrupt is rejected as an "ambiguous update"; folding it into
        # the resume Command applies it cleanly without disturbing the interrupt.
        command = (
            Command(resume=chosen_imdb_id, update={"auto_resolved": True})
            if auto_resolved
            else Command(resume=chosen_imdb_id)
        )
        final_state = await self._graph.ainvoke(
            command,
            config={
                **config,
                "run_name": f"resume {page_id}",
                "metadata": {
                    "page_id": page_id,
                    "origin": "auto_resolve" if auto_resolved else "resume",
                },
            },
        )
        return ResumeResult.from_state(final_state)

    async def auto_resolve_stale(self, max_age: float | None = None) -> StaleSummary:
        """Auto-resolve `awaiting_input` rows unanswered past the timeout (Phase 6d).

        Scans every row Notion reports as `awaiting_input`, reads each paused thread's
        checkpoint (keyed by `thread_id = page_id`), and for any that has sat longer than
        `max_age` seconds resumes it with the disambiguation pre-filter's stored
        `best_guess_imdb_id` at a trace-only `confidence=low` — so no Entry is ever stuck, and
        none is lost. `max_age` defaults to `STALE_INTERRUPT_TIMEOUT_SECONDS` (7 days; shorten
        via env to test).

        Age is taken from the checkpoint's own `created_at` — the paused thread's latest
        checkpoint *is* the `interrupt()` snapshot, so its timestamp is exactly the pause
        moment, independent of any later Notion edit to the row (and free — we must read the
        checkpoint anyway to recover the best guess).

        A row whose escalation was a *fail-safe* (LLM error / no valid index in `disambiguate`)
        carries no best guess: there is nothing to auto-pick, so it is left `awaiting_input`
        and logged (a human can still resume it any time) rather than guessed-at or `failed`.

        Like `resume()`, this takes no single-flight lock: it only ever touches the
        `awaiting_input` rows, which the reconcile sweep (pending/empty only) never sees.
        """
        assert self._graph is not None, "graph not compiled — use `async with Runtime()`"
        timeout = (
            max_age if max_age is not None else self._settings.STALE_INTERRUPT_TIMEOUT_SECONDS
        )
        entries = await self._notion.query_awaiting_input()
        log.info("stale-interrupt: %d awaiting_input row(s) to inspect", len(entries))
        now = datetime.now(UTC)
        counts: Counter[str] = Counter()
        for entry in entries:
            config: RunnableConfig = {"configurable": {"thread_id": entry.page_id}}
            state = await self._graph.aget_state(config)
            if not state.next:
                # Checkpoint already finished (Notion status lagged) — the next reconcile /
                # a re-resume is a no-op; nothing to do here.
                log.info(
                    "stale-interrupt: %r (%s) already resolved in checkpoint — skipping",
                    entry.title,
                    entry.page_id,
                )
                counts["already_done"] += 1
                continue
            age = self._checkpoint_age(state.created_at, now)
            if age is not None and age < timeout:
                counts["fresh"] += 1
                continue
            best_guess = state.values.get("best_guess_imdb_id")
            if not best_guess:
                # No stashed pick. Two shapes, distinguished by whether OMDb returned anything:
                #   - 0 candidates → the 6f not-found escalation: nobody claimed it past the
                #     timeout → presume genuinely nonexistent → resolve terminal `failed`.
                #   - >0 candidates → a disambiguate LLM fail-safe: real candidates exist, we
                #     just never got a confident pick → leave awaiting_input for a human (6d).
                if not state.values.get("candidates"):
                    log.info(
                        "stale-interrupt: %r (%s) not-found, unclaimed past timeout → failed",
                        entry.title,
                        entry.page_id,
                    )
                    await self.resume(entry.page_id, NOT_FOUND, auto_resolved=True)
                    counts["not_found"] += 1
                    continue
                log.warning(
                    "stale-interrupt: %r (%s) aged out but has no best guess — left "
                    "awaiting_input for a human",
                    entry.title,
                    entry.page_id,
                )
                counts["no_guess"] += 1
                continue
            log.info(
                "stale-interrupt: auto-resolving %r (%s) with best guess %s (age %.0fs)",
                entry.title,
                entry.page_id,
                best_guess,
                age if age is not None else -1,
            )
            await self.resume(entry.page_id, best_guess, auto_resolved=True)
            counts["resolved"] += 1
        summary = StaleSummary(
            total=len(entries),
            resolved=counts["resolved"],
            fresh=counts["fresh"],
            no_guess=counts["no_guess"],
            not_found=counts["not_found"],
            already_done=counts["already_done"],
        )
        log.info("stale-interrupt pass complete: %s", summary)
        return summary

    @staticmethod
    def _checkpoint_age(created_at: str | None, now: datetime) -> float | None:
        """Seconds since a checkpoint's ISO `created_at`, or None if it can't be parsed.

        An unparseable/absent timestamp returns None → treated as *aged out* (resolve it)
        rather than fresh, so a bad checkpoint can't strand a row in awaiting_input forever.
        """
        if not created_at:
            return None
        try:
            ts = datetime.fromisoformat(created_at)
        except ValueError:
            log.warning("stale-interrupt: unparseable checkpoint created_at %r", created_at)
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (now - ts).total_seconds()

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
                # Phase 6d: same cadence sweeps stale HITL interrupts (7-day timeout). Kept in
                # the same try so a failing pass is logged and the loop continues (ADR 0009).
                await self.auto_resolve_stale()
            except Exception:
                log.exception("reconcile cron cycle failed — continuing")
            await asyncio.sleep(period)

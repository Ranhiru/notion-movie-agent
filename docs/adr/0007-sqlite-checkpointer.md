# Durable execution via AsyncSqliteSaver, co-located with the app

LangGraph is compiled with `AsyncSqliteSaver` — a single `.sqlite` file living on the
same persistent volume as the app, keyed by `thread_id = page_id`. This is the
checkpointer that powers HITL (ADR 0006) and durable execution (learning target #3).

## Two state stores, deliberately

- **Notion `Enrichment Status`** owns **lifecycle** state — "which Titles need work."
  The reconcile reads it; it is the business source of truth.
- **The SQLite checkpoint file** owns **execution** state — the paused-run snapshot
  (OMDb candidate list, next node, partial lane results) needed to `Command(resume=...)`
  an interrupted graph.

These are not redundant: the `Status` column is a single enum and cannot hold graph
state, and the checkpoint file is not user-visible business state.

## Considered Options

- **Notion-only, no checkpointer (rejected):** hand-roll HITL by carrying candidate
  `imdbID`s in Slack button values and re-running the graph from scratch on click. Avoids
  the file, but discards both stated learning targets — LangGraph HITL (`interrupt()`
  *requires* a checkpointer) and durable execution.
- **Postgres checkpointer (rejected for now):** prod-grade and multi-process, but adds a
  database server — contrary to ADR 0002's single-process, run-it-locally goal. Documented
  upgrade path if ever going multi-process.
- **`AsyncSqliteSaver` file on the volume (chosen).**

## Consequences

- The volume **must be persistent** in every environment (local bind mount; platform
  volume in prod). A wiped volume orphans `awaiting_input` graphs — the stale-interrupt
  timeout (ADR 0006) is their cleanup.
- Low `max_concurrency` (3–5) keeps SQLite's single-writer limit a non-issue (WAL mode).
- Not a separate service — one file, created automatically, zero infra.

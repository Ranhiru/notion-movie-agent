# Enrichment status lifecycle: IMDb gates, RT is best-effort

A Title carries an `enrichment_status`: `pending` (default) → `done` | `failed`.
The reconcile query picks up only `pending` (or unset) Titles.

- **`done`** — OMDb resolved the title (IMDb rating + plot present) **and** the RT
  cascade ran to exhaustion. The two RT scores are best-effort and may be `null`; their
  absence does **not** block `done`, and RT is **never retried** after `done`.
- **`failed`** — OMDb **definitively** could not resolve the title (a real "not found").
- **`pending`** — new row, or a run that didn't complete: a process crash, or a
  **transient** OMDb error that `RetryPolicy` exhausted. Left `pending` on purpose so
  the hourly cron retries it.

## Considered Options

- **Strict `done` (IMDb *and* RT both required) (rejected):** RT genuinely doesn't
  exist for many titles (OMDb never has RT for TV; RT doesn't cover obscure films), so
  this would permanently `fail` legitimately-enriched titles, and a transient RT blip
  would become a permanent failure — defeating the cron self-healing in
  [0001](./0001-unified-reconcile-single-flight.md). Briefly chosen, then reversed.
- **IMDb gates, RT best-effort (chosen).**

## Consequences

- Accepted trade-off: if the entire RT cascade *transiently* fails in a run, the Title
  is still `done` with `null` RT and is never retried. Consistent with "RT best effort."
- The transient-vs-definitive distinction is critical: writing `failed` on a transient
  error (instead of leaving `pending`) would silently break self-healing. `failed` is
  reserved for definitive core failure only.
- Always write whatever partial data was obtained (idempotent upsert by `page_id`)
  before setting status, so a status of `done`/`failed` never discards found data.

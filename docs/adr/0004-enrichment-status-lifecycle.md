# Enrichment status lifecycle: IMDb gates, RT is best-effort

An Entry carries an `enrichment_status`: `pending` (default) → `done` | `failed`.
The reconcile query picks up only `pending` (or unset) Entries.

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
  this would permanently `fail` legitimately-enriched entries, and a transient RT blip
  would become a permanent failure — defeating the cron self-healing in
  [0001](./0001-unified-reconcile-single-flight.md). Briefly chosen, then reversed.
- **IMDb gates, RT best-effort (chosen).**

## Consequences

- Accepted trade-off: if the entire RT cascade *transiently* fails in a run, the Entry
  is still `done` with `null` RT and is never retried. Consistent with "RT best effort."
- The transient-vs-definitive distinction is critical: writing `failed` on a transient
  error (instead of leaving `pending`) would silently break self-healing. `failed` is
  reserved for definitive core failure only.
- Always write whatever partial data was obtained (idempotent upsert by `page_id`)
  before setting status, so a status of `done`/`failed` never discards found data.

## Amendment (Phase 6f) — a 0-result escalates before it `fail`s

An OMDb 0-result is *most often* a title-matching miss (a season suffix, `and`/`&`,
punctuation, a misspelling, a regional title), not a genuine "doesn't exist". So the
definitive-not-found path is now: `omdb_search` first retries mechanical
`normalize_title` fallbacks; a still-empty result is **not** written `failed` — it is
escalated to the HITL picker ([0006](./0006-hitl-disambiguation-out-of-band-resume.md),
manual-input-only) so a human can supply the imdbID search never surfaced. `failed`
still means "definitively not-found", but the determination is now human-confirmed:
either a human declines to resolve it, or — for an unclaimed escalation — the 7-day
stale-interrupt timeout resumes it to `failed` (a `NOT_FOUND` sentinel). This *tightens*
"definitive" rather than loosening it; a blank Entry and a transient error are unchanged
(blank → `failed` at `read_page`; transient → left `pending`).

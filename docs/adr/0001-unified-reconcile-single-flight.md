# Unified reconcile with single-flight lock and a cron safety net

All triggers funnel into one `reconcile()` entrypoint that queries the Notion data
source for Titles still needing enrichment and runs each through the enrichment graph.
A single-flight lock allows only one reconcile at a time; while one is running, extra
webhooks **ack 200/201, log, and drop** (no "rerun pending" flag). An **hourly cron**
re-runs reconcile to pick up any dropped or mid-sweep Titles (stragglers).

## Considered Options

- **Per-row webhook + sweep-only cron (rejected):** webhook enriches only its own
  `page_id`; cron owns the sweep. Lower straggler latency, but allows concurrent
  per-row runs.
- **Unified reconcile + drop extras + cron (chosen).**

## Consequences

- The deciding factor is **rate limiting**: funneling everything through one
  sequential, concurrency-capped reconcile prevents N rows added in a minute from
  fanning out into N×concurrency parallel API calls and tripping Notion/Firecrawl
  limits. This matters more than straggler latency.
- Trade-off accepted: a Title added during an active reconcile waits up to ~1 hour
  (next cron) instead of seconds. Fine for a personal DB. Cheap future upgrade is a
  single "rerun pending" boolean (still sequential, no new parallelism).
- The Notion sweep query is cheap — one filtered, server-side query returns only
  `pending` Titles (≤100 per page), not the whole table.

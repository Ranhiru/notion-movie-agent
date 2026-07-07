# Resilience: transient retry in two layers, plus three-layer backpressure

Phase 7 hardens the sweep against transient failures (network blips, 429s, 5xx) and bounds
the call rate to each external API. One predicate defines "transient" (`resilience.is_transient`
— `httpx.TransportError`, or an `HTTPStatusError` with status in `{429, 500, 502, 503, 504}`;
**not** `ValueError` / a pydantic `ValidationError` / a 4xx / a `KeyError`). Retrying a
deterministic error just burns quota to fail identically, so those fail fast.

## Retry lives in two layers, chosen per node semantics

A langgraph node-level `RetryPolicy` only fires when the node **raises**, and on exhaustion it
**re-raises** out of the node. That behaviour is correct for some nodes and wrong for others:

- **Gating nodes** — `read_page`, `omdb_search`, `omdb_details`, `update_notion` (Notion + OMDb).
  A transient error here *should* retry, and if it still exhausts, *should* propagate: the sweep
  (`_run_one`) catches it, writes nothing, and the Entry keeps its `pending`/unset status for the
  next cron pass ([0004](./0004-enrichment-status-lifecycle.md)). → **node-level `RetryPolicy`**
  (`transient_retry_policy`, exp backoff + jitter), applied to exactly these four nodes in
  `build_graph`.
- **Best-effort nodes** — the `rt` subgraph, and the LLM nodes (`disambiguate` / `resolve_rt` /
  `judge`). These must **never** raise a transient out: RT must not block `done`
  ([0003](./0003-rt-resolution-fallback-chain.md) / 0004), and the LLM nodes fail *safe* (defer
  the pick) or degrade (`confidence=low`). A node-level RetryPolicy whose exhaustion re-raises
  would break those invariants. So retry sits **inside the provider, below the swallow**:
  - `FirecrawlClient._search` retries transient failures internally (honoring `Retry-After` on
    429, exp backoff otherwise) up to `RETRY_MAX_ATTEMPTS`, then raises — and `firecrawl_provider`
    swallows that to null RT. A momentary Firecrawl hiccup no longer permanently nulls RT.
  - `ChatOpenAI`'s built-in `max_retries` (`LLM_MAX_RETRIES`) covers transient LLM blips before
    the node's own `except` degrades the result.

This is the crux: *where* retry lives is dictated by whether an exhausted retry should fail the
Entry (gating → node RetryPolicy) or degrade a best-effort field (→ client-internal retry).

## Three-layer backpressure

Raising `RECONCILE_CONCURRENCY` (the Phase 8 goal) must not fan out into an API 429-storm, so
call rate is bounded at three composable layers:

1. **Across Entries** — the sweep semaphore (`RECONCILE_CONCURRENCY`), unchanged from Phase 3.
2. **Within an Entry** — `GRAPH_MAX_CONCURRENCY` caps parallel-node execution per `ainvoke` (the
   fan-out lanes). `0` = unset.
3. **Per API** — process-global `aiolimiter`s: Notion (`NOTION_RPS`, Phase 3) and Firecrawl
   (`FIRECRAWL_RPM`, new); plus one shared `InMemoryRateLimiter` (`LLM_RPS`, `0` = off) capping
   the **aggregate** rate across all three role models, which share one `OPENAI_BASE_URL`.

Tavily + Exa limiters land with those clients in Phase 8; they reuse this machinery.

## Batch isolation

`_sweep` gathers per-Entry runs with `return_exceptions=True` and maps any escaped exception to a
transient tally (`_classify_result`), so one Entry's unexpected error (or a `CancelledError`)
can't cancel the whole gather and abort its siblings. Per-Entry *state* is already isolated —
each run checkpoints under `thread_id = page_id` ([0007](./0007-sqlite-checkpointer.md)) — so this
only guards the *scheduling* of the batch; there is no shared transaction to roll back.

## Considered Options

- **Node `RetryPolicy` everywhere (rejected):** on the best-effort nodes, an exhausted retry
  re-raises and fails the Entry — breaking "RT never blocks `done`" and the LLM fail-safe.
- **Client-internal retry everywhere (rejected):** the gating nodes *want* propagation on
  exhaustion (→ `pending` → cron self-heals); burying retry in the client and swallowing there
  would silently drop Entries that should be retried next pass.
- **Two-layer, split by node semantics (chosen).**

## Consequences

- New config knobs (all defaulted, `0`/`0.0` = unset per the existing `OPENAI_MAX_TOKENS` idiom):
  `RETRY_MAX_ATTEMPTS`, `FIRECRAWL_RPM`, `LLM_RPS`, `LLM_MAX_RETRIES`, `GRAPH_MAX_CONCURRENCY`.
- The gating-node RetryPolicy and the Firecrawl/Notion client loops share `is_transient` /
  `TRANSIENT_STATUS`, so "what is worth retrying" has one definition.
- Retries add latency to a genuinely-down dependency (bounded by `RETRY_MAX_ATTEMPTS` × backoff)
  before an Entry falls back to `pending` — an accepted trade for absorbing the common blip.

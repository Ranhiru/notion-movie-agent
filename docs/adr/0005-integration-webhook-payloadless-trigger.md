# Notion integration webhook on `page.created` as an unverified payload-less trigger

> **Status: deferred.** Sequenced to the deploy milestone (see
> [0009](./0009-local-first-cron-only-slack-socket-mode.md)); the local-first build uses
> a cron-only trigger. This ADR records the design for when/if the webhook is added.

New Entries are signalled via a **Notion integration webhook** (created in the
integration's connection settings), subscribed to **`page.created`**. The handler
**skips signature verification** and **ignores the payload** — it simply triggers a
reconcile (subject to the single-flight lock of
[0001](./0001-unified-reconcile-single-flight.md)) and returns 200. The one-time
`verification_token` handshake is done only to *activate* the subscription.

## Considered Options

- **Integration webhook with HMAC-SHA256 verification (rejected for simplicity):** meets
  the original §5 security bar, but adds verification code. Consciously dropped — see
  Consequences for why the residual risk is low.
- **Notion UI "automation" webhook (rejected — unavailable):** requires a **paid** Notion
  plan (`Send webhook` is paid-only; free plans get Slack automations only). We are on
  the free plan, and the integration webhook is free on any plan.
- **Integration webhook, unverified, payload-less trigger (chosen).**

## Consequences

- **Security trade-off accepted.** The endpoint is unauthenticated, but the blast radius
  is small *by construction*: the payload is ignored (no injection), and the expensive
  enrichment providers run only for `pending` Entries — which an attacker cannot create
  (only adding rows in Notion does). So endpoint spam cannot burn OMDb/Firecrawl/search
  credits; worst case is repeated **no-op** reconcile sweeps (one cheap, free Notion
  query each), bounded further by the single-flight lock. Reversible later by adding the
  HMAC check.
- `page.created` is **aggregated** by Notion (batched, up to ~1 min delay, sometimes
  dropped) — best-effort by design, so the hourly cron (ADR 0001) remains the real
  correctness guarantee.
- Requires the `Notion-Version: 2025-09-03` header and storing the **`data_source_id`**;
  the reconcile query hits `POST /v1/data_sources/{id}/query`.
- If the subscription is recreated, the `verification_token` rotates (only matters for
  re-activating the subscription, since we don't verify per-event).

# Local-first build: cron-only trigger, Slack Socket Mode for HITL, webhook deferred

The build runs as a **local Docker container** with **no inbound HTTP endpoints**, kept up by
**`docker compose` with `restart: always`**. Because Slack Socket Mode covers all interaction
(see [0010](./0010-slack-bolt-socket-mode.md)) and the hourly cron is the correctness
guarantee, **the Notion webhook is dropped, not merely deferred** — there is no planned public
host. Secrets use **Docker Compose secrets** (file-mounted) for the compose deployment; plain
`.env` for non-Docker local dev.

- **Trigger = in-process cron only** (plus a manual "run now"). The Notion integration
  webhook ([0005](./0005-integration-webhook-payloadless-trigger.md)) is **deferred** —
  possibly indefinitely, since the cron is the correctness guarantee
  ([0001](./0001-unified-reconcile-single-flight.md)) and the webhook was only ever a
  latency optimization. With it deferred, the early app needs no FastAPI inbound server.
- **HITL transport = Slack Socket Mode.** Disambiguation escalation
  ([0006](./0006-hitl-disambiguation-out-of-band-resume.md)) needs to *receive* the
  human's button click — which is inbound. Socket Mode opens an **outbound WebSocket**
  from the app to Slack (app-level token), so clicks arrive over that connection with
  **no public endpoint and no tunnel**, working inside local Docker.

## Considered Options

- **Slack HTTP request URL (rejected):** would require a public endpoint or an ngrok
  tunnel even in local dev — the exact inbound config we're avoiding early.
- **Socket Mode (chosen):** zero inbound endpoints in the early build, and it works
  identically in production, so the Slack transport never has to migrate.

## Consequences

- The entire early build has **no inbound HTTP** — just a long-lived process: in-process
  cron → `reconcile()` → LangGraph workflow → SQLite checkpointer, plus an outbound
  Slack socket for HITL.
- Posting the disambiguation prompt is an ordinary outbound Slack API call; receiving the
  click comes back over the socket.
- The public-HTTPS question (and the Notion webhook) is confined to the deploy milestone.

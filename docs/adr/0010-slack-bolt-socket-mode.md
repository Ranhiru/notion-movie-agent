# Slack (Bolt + Socket Mode) as the only inbound path: HITL picks + manual run

The app uses the **Slack Bolt** framework in **Socket Mode** — a single outbound WebSocket
to Slack that carries *all* inbound interaction, so the build needs no public HTTP endpoint
(see [0009](./0009-local-first-cron-only-slack-socket-mode.md)). It serves two purposes:

1. **HITL disambiguation** ([0006](./0006-hitl-disambiguation-out-of-band-resume.md)): post
   the candidate Titles to **`#notion-movie-db`**, receive the button click, resume the graph.
2. **Manual run**: an app-mention `@movie-bot run` triggers `reconcile()` (under the
   single-flight lock of [0001](./0001-unified-reconcile-single-flight.md)). The hourly cron
   covers everything else.

## Tokens & config

- **`SLACK_BOT_TOKEN`** (`xoxb-…`) and **`SLACK_APP_TOKEN`** (`xapp-…`, scope
  `connections:write`) — both required for Bolt Socket Mode. Socket Mode + Interactivity must
  be enabled in the app config; `app_mentions:read` for the manual-run trigger.
- All Slack credentials loaded from env (`.env` in development).

## Block Kit message shape

Per the picker mockup: up to **5 candidates**, each a `section` with the **title** (bold),
**plot summary**, and the **poster image** as an `accessory` (OMDb `Poster` URL when not
`N/A`), followed by one `actions` block with up to **5 buttons** (one per candidate).

## Mapping a click back to the right Title

Each button's `value` encodes **`page_id` + chosen `imdbID`** (the `page_id` is the graph's
`thread_id`). On click, Bolt's action handler calls
`graph.invoke(Command(resume=<imdbID>), thread_id=<page_id>)`, resuming the correct
interrupted run. Because resume on a finished thread is a no-op, double-clicks are safe.

## Consequences

- The entire app's inbound surface is this one WebSocket; no tunnel, no public host, works in
  local Docker.
- The manual `@movie-bot run` and the cron both funnel into the same single-flight
  `reconcile()` — no new concurrency path.

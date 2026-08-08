# Slack (Bolt + Socket Mode) as the only inbound path: HITL picks + mention commands

The app uses the **Slack Bolt** framework in **Socket Mode** — a single outbound WebSocket
to Slack that carries *all* inbound interaction, so the build needs no public HTTP endpoint
(see [0009](./0009-local-first-cron-only-slack-socket-mode.md)). It serves three purposes:

1. **HITL disambiguation** ([0006](./0006-hitl-disambiguation-out-of-band-resume.md)): post
   the candidate Entries to **`#notion-movie-db`**, receive the button click, resume the graph.
2. **Manual run**: an app-mention `@movie-bot run` triggers `reconcile()` (under the
   single-flight lock of [0001](./0001-unified-reconcile-single-flight.md)). The hourly cron
   covers everything else.
3. **Add an Entry**: `@NotionMovieAgent add <name>` dispatches from the same `app_mention`
   listener and starts the out-of-band create-then-enrich flow ([0012](./0012-slack-add-command-second-entry-point.md)).

## Tokens & config

- **`SLACK_BOT_TOKEN`** (`xoxb-…`) and **`SLACK_APP_TOKEN`** (`xapp-…`, scope
  `connections:write`) — both required for Bolt Socket Mode. Socket Mode + Interactivity must
  be enabled in the app config; subscribe to the `app_mention` bot event and grant
  `app_mentions:read` plus `chat:write` for mention commands and their replies.
- All Slack credentials loaded from env (`.env` in development).

## Block Kit message shape

Per the picker mockup: up to **5 candidates**, each a `section` with the **title** (bold),
**plot summary**, and the **poster image** as an `accessory` (OMDb `Poster` URL when not
`N/A`), followed by one `actions` block with up to **5 buttons** (one per candidate), followed
by an **inline manual-input escape hatch** (see [0006](./0006-hitl-disambiguation-out-of-band-resume.md)):
an `input` block with a `plain_text_input` (`dispatch_action: true`, submit on Enter,
`action_id` outside the `pick:\d+` namespace) labeled "None of the above? Paste the IMDb
link" — for when the right title isn't among the candidates. **Caveat:** `chat_update` on a
message wipes any half-typed input value, so the picker is only ever updated on terminal
resolution, never mid-wait.

## Mapping an answer back to the right Entry

Each button's `value` encodes **`page_id` + chosen `imdbID`** (the `page_id` is the graph's
`thread_id`). On click, Bolt's action handler calls
`graph.invoke(Command(resume=<imdbID>), thread_id=<page_id>)`, resuming the correct
interrupted run. Because resume on a finished thread is a no-op, double-clicks are safe.

The manual input's `dispatch_action` event lands in a separate handler that extracts the
imdbID (`tt\d+`) from the pasted text (full URL or bare id) and resumes through the **same**
`Command(resume=<imdbID>)` path — on unparseable text it replies with a correction hint and
leaves the picker up (message inputs have no modal-style inline validation). The `page_id`
rides in the input block's `block_id`, since an `input` element has no `value` field of its
own.

## Consequences

- The entire app's inbound surface is this one WebSocket; no tunnel, no public host, works in
  local Docker.
- The manual `@movie-bot run` and the cron both funnel into the same single-flight
  `reconcile()` — no new concurrency path.

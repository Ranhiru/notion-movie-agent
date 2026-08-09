# Slack mention `add`: a second entry point via create-then-enrich, out-of-band

A Slack app mention `@NotionMovieAgent add <name>` lets a human originate an Entry instead of
adding a row in Notion. Rather than invert the architecture, it is an **additional entry
point**: the handler creates the Notion page exactly as a human would (Entry only, status
`pending`), then enriches that one `page_id` **out-of-band** — directly via the graph, *not*
under the reconcile single-flight lock — reusing the whole existing pipeline (OMDb,
disambiguation, RT, Judge, write-back) unchanged because by enrich time a real `page_id`
exists. This keeps `thread_id = page_id`, the status lifecycle, the checkpointer, and the
HITL Slack picker all intact ([0001](./0001-unified-reconcile-single-flight.md),
[0004](./0004-enrichment-status-lifecycle.md),
[0006](./0006-hitl-disambiguation-out-of-band-resume.md),
[0010](./0010-slack-bolt-socket-mode.md)).

## Considered Options

- **Invert the entry point (rejected):** run the graph from Slack with no page, create a
  fully-populated page at the end. Avoids a transient `pending` row, but there is no
  `page_id` during the run, so `thread_id`, the checkpointer key, HITL interrupt/resume,
  and reconcile discovery all need a parallel identity scheme — a large divergence from
  ADR 0006/0007 for a cosmetic gain. Rejected.
- **Create page first, then trigger `reconcile()` (rejected):** fully reuses ADR 0001, but
  Slack `add` is interactive and ADR 0001 accepts that an Entry added during an active sweep waits
  up to ~1 hour for the next cron — poor UX for someone watching Slack.
- **Create page first, enrich that one page out-of-band (chosen).**

## How it coexists with the sweep — the de-confliction hazard

The out-of-band enrich runs on a **`pending`** page — the same pool the reconcile sweep
queries (`is_empty OR equals pending`). Unlike a HITL resume (which is safe because its row
is `awaiting_input`, a status the sweep ignores — ADR 0006), nothing in the status alone
keeps a concurrent cron sweep from selecting and re-enriching the same `pending` row, which
would double external-API spend (exactly what ADR 0001 exists to prevent).

De-confliction is a **process-global in-flight `page_id` guard** (a set / per-page lock),
shared by the sweep and the Slack-add path; the sweep skips any `page_id` currently being
enriched out-of-band. No new Notion status, because ADR 0009 makes this a single process.
It is **crash-safe by construction**: if the process dies mid-enrich the set vanishes but
the row is still `pending`, so the next cron sweep reclaims it — ADR 0004 self-healing is
preserved, which a new `enriching` status would have broken (a crash would strand the row).

## Completion feedback and `origin`

The app-mention handler schedules create/dedupe/enrich in a background task and returns
promptly. It opens a `chat.startStream` reply beneath the mention with
`task_display_mode="plan"`, the originating workspace/user recipient IDs, and a grouped plan
covering deduplication, Notion creation, database search, title matching/disambiguation, OMDb,
Rotten Tomatoes, source comparison, verification, confidence, and the final Notion update.
The handler's Bolt `set_status` utility displays `Adding {title} to your watchlist…` and rotates
ten production-themed loading messages while that plan runs.

`Runtime.create_and_enrich` retains LangGraph `stream_mode="updates"`, but its progress callback
now emits stable, transport-neutral event IDs. The Slack transport maps those IDs to
`task_update` chunks (`pending` → `in_progress` → `complete`, or `error`) and sends every
transition with `chat.appendStream`. Every successfully opened stream is terminated with
`chat.stopStream` on success, duplicate detection, HITL pause, or failure. Streaming and status
calls remain best-effort: any Slack failure is logged and never gates durable enrichment.

The loading-message rotation is:

1. “Rewinding the tape for clues…”
2. “Checking continuity with the script supervisor…”
3. “Sending the title through casting…”
4. “Asking the projectionist to focus…”
5. “Searching the backlot for the right cut…”
6. “Comparing notes in the writers’ room…”
7. “Checking whether this is the reboot…”
8. “Waiting for the post-credits scene…”
9. “Polishing the ratings montage…”
10. “Rolling credits on the paperwork…”

The original mention's thread-root timestamp is carried in graph state with the Slack
channel/user. The terminal `notify` node posts the completion result (IMDb / RT / genre) or
not-found notice as a reply beneath that mention. Persisting the timestamp matters because
the terminal state may arrive much later — after a Slack disambiguation click or even the
7-day cron auto-resolve. The node is gated on `origin == "slack"`: it posts only for
`slack`-originated runs and no-ops for sweep rows. Runs created before this field existed and
CLI runs lack a thread root, so they fall back to a channel `chat_postMessage`.

That same checkpointed channel/thread context is supplied to the interrupt notifier. A
Slack-originated add therefore posts its HITL picker in the original mention thread. Scheduled
and manual reconcile rows have no originating request thread, so their picker remains a
top-level message in the configured HITL channel.

## Consequences

- **New surface, reused core.** New: `add` dispatch in the existing `app_mention` listener
  (delivered over the Socket Mode socket — ADR 0010), the create-page step, a best-effort pre-create
  dedupe query (match the typed Entry case-insensitively; on a hit, tell the user and
  create nothing), the in-flight guard, streamed task-plan updates, origin-aware HITL routing,
  the notify node, and durable graph state for `origin` plus Slack notify/thread context.
  Unchanged: the enrichment graph
  topology, disambiguation picker, checkpointer, status lifecycle, single-flight lock, rate
  limiters.
- **The agent now writes `Type`.** §8 had `Type` as human-filled; for Slack-add rows it starts
  blank (search OMDb unfiltered, let the 1/many + disambiguation logic resolve `media_type`)
  and is backfilled from the resolved `media_type` so the row ends up complete.
- **Dedupe is best-effort only.** It matches the typed string before the canonical title /
  `imdb_id` exist, so it catches exact re-adds but not variant spellings (*Dune* vs *Dune:
  Part Two*); those remain the user's call. No page deletion is introduced.
- **No Notion schema change.** Slack `add` populates exactly the existing §8 enrichment fields
  (IMDb rating, RT critic + audience, plot, genre, Enrichment Status, + backfilled Type).
- Sequenced as the phase immediately **before** the deploy phase (TASKS Phase 9).

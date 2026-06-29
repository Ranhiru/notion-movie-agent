# Slack `/add` command: a second entry point via create-then-enrich, out-of-band

A Slack slash command `/add <name>` lets a human originate a Title from Slack instead of
adding a row in Notion. Rather than invert the architecture, it is an **additional entry
point**: the handler creates the Notion page exactly as a human would (Title only, status
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
  `/add` is interactive and ADR 0001 accepts that a Title added during an active sweep waits
  up to ~1 hour for the next cron — poor UX for someone watching Slack.
- **Create page first, enrich that one page out-of-band (chosen).**

## How it coexists with the sweep — the de-confliction hazard

The out-of-band enrich runs on a **`pending`** page — the same pool the reconcile sweep
queries (`is_empty OR equals pending`). Unlike a HITL resume (which is safe because its row
is `awaiting_input`, a status the sweep ignores — ADR 0006), nothing in the status alone
keeps a concurrent cron sweep from selecting and re-enriching the same `pending` row, which
would double external-API spend (exactly what ADR 0001 exists to prevent).

De-confliction is a **process-global in-flight `page_id` guard** (a set / per-page lock),
shared by the sweep and the slash path; the sweep skips any `page_id` currently being
enriched out-of-band. No new Notion status, because ADR 0009 makes this a single process.
It is **crash-safe by construction**: if the process dies mid-enrich the set vanishes but
the row is still `pending`, so the next cron sweep reclaims it — ADR 0004 self-healing is
preserved, which a new `enriching` status would have broken (a crash would strand the row).

## Completion feedback and `origin`

`/add` is interactive, so it acks within Slack's 3-second window (an ephemeral "Adding…"),
does the create/dedupe/enrich in a background task, and posts a completion message with the
results (IMDb / RT / genre) or a not-found notice. Because the run may reach its terminal
state much later — after a Slack disambiguation click or even the 7-day cron auto-resolve —
the notification cannot live at the slash handler's call site. It is a **terminal notify
node in the graph**, gated on graph state `origin` ∈ {`sweep`, `slack`}: it posts only for
`slack`-originated runs and no-ops for sweep rows. The Slack target (channel/user) is
carried in durable graph state alongside `origin` so it survives the interrupt/resume and a
process restart. Completion uses `chat_postMessage` (not the slash `response_url`, which
expires ~30 min / 5 uses) precisely because a run can sit in HITL for days.

## Consequences

- **New surface, reused core.** New: the `/add` command registration (delivered over the
  existing Socket Mode socket — ADR 0010), the create-page step, a best-effort pre-create
  dedupe query (match the typed Title case-insensitively; on a hit, tell the user and
  create nothing), the in-flight guard, the notify node, and two graph-state fields
  (`origin`, Slack notify context). Unchanged: the enrichment graph, disambiguation picker,
  checkpointer, status lifecycle, single-flight lock, rate limiters.
- **The agent now writes `Type`.** §8 had `Type` as human-filled; for `/add` rows it starts
  blank (search OMDb unfiltered, let the 1/many + disambiguation logic resolve `media_type`)
  and is backfilled from the resolved `media_type` so the row ends up complete.
- **Dedupe is best-effort only.** It matches the typed string before the canonical title /
  `imdb_id` exist, so it catches exact re-adds but not variant spellings (*Dune* vs *Dune:
  Part Two*); those remain the user's call. No page deletion is introduced.
- **No Notion schema change.** `/add` populates exactly the existing §8 enrichment fields
  (IMDb rating, RT critic + audience, plot, genre, Enrichment Status, + backfilled Type).
- Sequenced as the phase immediately **before** the deploy phase (TASKS Phase 9).

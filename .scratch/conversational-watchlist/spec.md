# Conversational Watchlist manager

Status: resolved

## Problem and solution

The personal Movie Bot needs natural-language Watchlist management: multi-title Adds,
URL Imports, unwatched Recommendations, viewing-state changes, removal, and refresh.
Use LangChain `create_agent` with dedicated tools backed by the existing Enrichment Runtime.
Application code, independently of model behaviour, enforces ownership, exact Action Plans,
approval, identity validation, and durable execution. Successful partial results survive.

## Conversation and tools

- Serve one configured Slack owner. A mention starts a conversation; ordinary replies
  continue an established bot thread. Ignore bot messages and unrelated thread traffic.
  Enforce ownership at both request and interactive-action boundaries.
- Persist context by workspace, channel, and thread. Persist the ordered Entry IDs behind
  displayed lists so numbered follow-ups survive restarts and remain isolated by thread.
  Recheck referenced Entries against their current state before acting.
- Expose Watchlist reads, identity resolution, Add, recoverable removal, watched/unwatched,
  refresh, web search, and URL list extraction. Recommendations and discovery compose reads
  and search; tools cannot perform unrestricted Notion writes or agent-driven schema changes.
- Prepare the complete Action Plan before writes. One request identity spans tool calls,
  approval, clarification, and recovery. Splitting calls or subplans cannot reset its count.
  Explicit continuations are new user instructions and pass the same policy.

## Identity and Enrichment

- Resolve each requested work before creating an Entry. Preserve compound names, support
  movies and TV shows, clarify uncertain identities, and keep same-name remakes distinct.
- Deduplicate by IMDb Identity, including alternate names. An already-present result identifies
  the existing Entry. Store resolved IMDb ID in the Watchlist; verify schema before adding
  the property through an explicit migration.
- Handle unidentified legacy Entries conservatively, bounded to 25. Require clarification
  or exact owner-bound identity confirmation when uncertain; never destructively merge data.
- Tools call the existing Runtime. Preserve page-keyed checkpoints, Disambiguation interrupts
  and resumes, Enrichment Status, Reconcile coordination, rate limits, progress, and late
  completion notifications. Notion-originated Enrichment continues working.
- Resolve and deduplicate conversational Adds before creating the pending Entry, then invoke
  page-based Enrichment. Page creation still precedes Enrichment, as revised in ADR 0012.
- Preserve ADR 0008's Judge against wrong identity and cross-lane mismatches. Carry the original
  user request into validation; the agent's proposed IMDb Identity is not ground truth.
- Keep conversation checkpoints separate from Notion Entry enrichment checkpoints.

## Approval and execution

- Count distinct affected Entries across the entire request, including new Entries by
  resolved IMDb Identity and mixed operations. Four or more require approval before any writes.
- Every URL Import requires a selectable preview, even for one title. Its approval can also
  authorize the displayed bulk operation. Execute only the selected actions.
- Bind approval to the exact actions, Entries, owner, and request. It is single-use and expires
  after 30 minutes. Reject changed, expired, replayed, cross-request, or unauthorized approvals.
  Unexpected additional targets require approval of a revised plan.
- Persist plans, approval state, references, and per-action progress using existing SQLite
  persistence. Serialize competing execution for the same request/Entry and keep retries
  idempotent. Recovery of an approved execution is not another use of its approval.
- Reconcile uncertain external-write outcomes after a crash or timeout before repeating writes.
  A successful remote create without a local receipt must not create a duplicate.
- Preserve successful actions when siblings fail or await clarification. Report each requested
  title as added, already-present, awaiting clarification, or failed in its original thread.
  Do not introduce rollback or deletion of successful additions.

## Recommendations, research, and URL Imports

- Default to up to five varied existing unwatched Entries, fewer when the eligible pool is
  smaller. Use the authoritative Watched checkbox independently of Enrichment Status; missing
  or malformed values are unknown, not evidence that an Entry is unwatched.
- Respect explicit preferences and thread follow-ups. Give brief reasons using stored genre,
  plot, and ratings. Keep critic and audience scores distinct and acknowledge missing evidence.
- Search for specific factual questions or material evidence gaps, citing supporting links.
  Explicit external discovery may suggest outside titles, clearly labelled; a Recommendation
  or discovery does not authorize an Add.
- Read the supplied URL once and extract the intended list, excluding unrelated page links.
  Explain unreadable or ambiguous pages rather than inventing titles; ask for an accessible URL
  or clarification. Do not crawl other pages or bypass inaccessible content.
- Treat page text and search results as untrusted data. They cannot amend instructions,
  authorize writes, or expand an approved Action Plan.
- Validate retrieval URLs and redirects at the trust boundary; disallow local/private network
  resources. Permit public web content without exposing internal resources.
- Initial ceilings are 25 extracted titles and three web searches per request. Explain reached
  limits and offer explicit continuation, without silent truncation. Increase ceilings only
  deliberately after observing behaviour; continuation retains all approval and URL safeguards.

## Write semantics and scope

- Removal is recoverable Notion soft deletion only; permanent deletion is excluded.
- Watched/unwatched changes update only viewing state, preserving metadata.
- Refresh can replace machine-managed ratings, plot, and genre while preserving Watched and
  Entry identity. A changed identity requires clarification.
- Tracking is at Entry level, including TV shows; season/episode tracking is excluded.
- Exclude multiple users, separate viewing histories, persistent taste profiles, autonomous
  preference learning, automatic merging, and manual rating/plot/title/type editing.
- Exclude unsolicited external discovery and automatic additions from recommendations.
- Exclude unsupported streaming-availability claims and unrelated Watchlist features.
- Deep Agents, framework comparisons, shell/filesystem tools, and unrestricted crawling are
  outside scope. Revisit the harness only when new requirements justify those capabilities.

## Testing and acceptance

The confirmed primary seam is complete Slack conversation requests through the existing
transport-to-Runtime boundary, observing replies and Watchlist effects. Extend asynchronous
`unittest` tests, fixture-backed services, and scripted adversarial agent calls. Prefer this
seam over tests coupled to prompts, internal graph nodes, or dispatch details. Recovery tests
use real durable conversation state across restarts; installed-agent tests exercise tool calls.

Required cases:

- Ownership, mention/thread routing, context isolation, and persistent numbered references.
  Removed/changed targets are rechecked; invalid references cannot shift displayed numbering.
- Multiple named Adds, compound names, aliases, remakes, ambiguous identities, already-present
  results, legacy Entries without IMDb ID, and integration with existing Enrichment.
- Three affected Entries may proceed; four require approval before writes. Several smaller
  calls and mixed actions cannot bypass the threshold. URL Imports preview even one title.
- Exact selection, expiry, replay, changed-plan, cross-request, and unauthorized approvals.
  Duplicate Slack delivery, approval restart, partial-execution restart, concurrent attempts,
  and a remote write completed before its receipt must preserve effects without duplication.
- Untrusted page/search instructions cannot authorize tools. Extraction excludes unrelated
  links. Blocked/unreadable pages give useful feedback. Initial URLs and redirects cannot reach
  private resources; search/extraction ceilings produce explicit continuation notices.
- Recommendations exclude Watched Entries, respect preferences, return fewer than five when
  necessary, disclose missing metadata, cite web evidence, and never add external suggestions
  without a request.
- Recoverable soft removal, watched/unwatched metadata preservation, refresh identity/viewing
  preservation, and partial failures with per-Entry outcomes.
- Runnable deterministic checks, formatting, lint, and types. Static checks do not execute
  tests. Keep real-model evaluation opt-in and distinct from deterministic policy checks;
  compare expected outcomes rather than exact prose or internal call order.
- Model evaluation uses fixture-backed tools without production writes to demonstrate intent
  interpretation and useful responses. Subsequent live checks use controlled Slack interactions
  and a dedicated Notion test database for API behaviour, removal, approval, Enrichment, and
  restart durability. State blocked validation explicitly.

## Implementation validation

The manager, application-owned approvals, durable references and receipts, bounded research,
and shared Enrichment integration are implemented. Deterministic Slack-to-Runtime and
installed-agent tests, static checks, and Compose configuration checks cover the implementation.
This spec records agreed requirements; its publication alone did not authorize implementation.

At implementation validation, Watched was verified read-only and IMDb ID was missing. Live
activation remained pending: configure the model and owner, prepare the identity property,
and enable Slack thread subscriptions as documented in README.md. The configured model
endpoint was unreachable, so real-model evaluation did not pass and controlled Slack/Notion
mutation tests were not run. No production Watchlist Entries or schema were changed.

# Conversational Watchlist manager

Status: resolved

## Problem Statement

The user's personal Movie Bot accepts rigid single-title add commands. Adding several
movies or TV shows requires repeated requests, importing titles from an article requires
manual copying, and choosing something unwatched requires manually browsing the Watchlist.
The bot cannot carry on a conversation or manage viewing state and removal through Slack.

The user wants natural-language Watchlist management that reuses existing Enrichment,
recognizes movie identity reliably, and performs only the changes they intended. Larger
requests must require explicit approval, successful additions must survive partial failures,
and conversations and execution progress must survive restarts.

## Solution

Introduce a personal conversational Movie Bot using LangChain `create_agent`, dedicated
Watchlist tools, and the existing Enrichment Runtime. A mention starts a Slack conversation;
ordinary replies continue it in the same thread. The bot can add multiple movies or TV
shows, preview URL Imports, recommend unwatched Entries, discover external titles on request,
soft-remove Entries, mark them watched or unwatched, and refresh their Enrichment.

The application enforces approval across the entire request, independently of model behavior.
Requests affecting four or more distinct Entries require an approved action preview before
any writes. Every URL Import requires preview approval regardless of size. IMDb Identity
drives deduplication, and durable thread references make follow-ups such as "mark the second
one watched" resolve to the Entries actually displayed.

## User Stories

1. As the Watchlist owner, I want to start a conversation by mentioning Movie Bot, so that I can use it naturally in Slack.
2. As the Watchlist owner, I want ordinary replies to continue an established bot thread, so that I do not have to mention the bot repeatedly.
3. As the Watchlist owner, I want separate Slack threads to retain separate context, so that unrelated conversations do not interfere.
4. As the Watchlist owner, I want thread context to survive bot restarts, so that follow-up requests remain meaningful.
5. As the Watchlist owner, I want only my configured Slack user to access the bot's capabilities, so that workspace membership changes do not expose my Watchlist.
6. As the Watchlist owner, I want to add a movie using natural language, so that rigid command syntax is unnecessary.
7. As the Watchlist owner, I want to add several named movies in one request, so that I can capture a list quickly.
8. As the Watchlist owner, I want to add TV shows as well as movies, so that the bot manages my complete Watchlist.
9. As the Watchlist owner, I want the bot to preserve compound movie names, so that conjunctions inside a title do not create unintended Entries.
10. As the Watchlist owner, I want uncertain title identities clarified, so that the correct movie or show is added.
11. As the Watchlist owner, I want alternate names for the same IMDb Identity recognized as duplicates, so that aliases do not clutter my Watchlist.
12. As the Watchlist owner, I want same-name remakes kept distinct, so that different works are not incorrectly merged.
13. As the Watchlist owner, I want an already-present result to identify the existing Entry, so that I understand why no new Entry was created.
14. As the Watchlist owner, I want new Entries enriched using the existing pipeline, so that ratings, plot, genre, and Disambiguation remain consistent.
15. As the Watchlist owner, I want existing Notion-originated Enrichment to continue working, so that Slack functionality does not disrupt Reconcile.
16. As the Watchlist owner, I want to supply a URL containing a movie or TV list, so that I do not have to copy every title.
17. As the Watchlist owner, I want URL Import to extract the intended list rather than unrelated page links, so that only relevant titles are proposed.
18. As the Watchlist owner, I want a selectable preview before every URL Import, so that I decide which titles enter my Watchlist.
19. As the Watchlist owner, I want unreadable or ambiguous pages explained, so that failed extraction does not silently create guessed Entries.
20. As the Watchlist owner, I want page text treated as data rather than instructions, so that a web page cannot authorize Watchlist changes.
21. As the Watchlist owner, I want five varied unwatched recommendations by default, so that I can quickly choose something to watch.
22. As the Watchlist owner, I want the existing Watched checkbox to determine eligibility, so that recommendations reflect my recorded viewing state.
23. As the Watchlist owner, I want recommendations to use stored genre, plot, and ratings with brief reasons, so that I understand each choice.
24. As the Watchlist owner, I want recommendations to respect explicit preferences, so that requests such as "something lighter" narrow the choices.
25. As the Watchlist owner, I want recommendations to acknowledge missing evidence, so that invented metadata does not influence my choice.
26. As the Watchlist owner, I want web search when answering a specific question or filling an evidence gap, so that the bot can supplement stored information.
27. As the Watchlist owner, I want supporting links for web-derived claims, so that I can assess the evidence.
28. As the Watchlist owner, I want explicit discovery requests to suggest titles outside my Watchlist, so that I can find new things to watch.
29. As the Watchlist owner, I want external suggestions labeled and added only when requested, so that discovery does not change my Watchlist unexpectedly.
30. As the Watchlist owner, I want to refer to numbered recommendations in follow-ups, so that I can act without retyping titles.
31. As the Watchlist owner, I want referenced Entries checked against their current state, so that stale conversation context does not cause an incorrect operation.
32. As the Watchlist owner, I want to mark Entries watched or unwatched, so that I can manage viewing state from Slack.
33. As the Watchlist owner, I want removal to be recoverable, so that a mistaken removal does not permanently destroy an Entry.
34. As the Watchlist owner, I want refresh to replace machine-managed metadata while preserving Watched and Entry identity, so that I can update information without changing viewing history.
35. As the Watchlist owner, I want changed identity during refresh to require clarification, so that an existing Entry is not silently repurposed.
36. As the Watchlist owner, I want a complete action preview before a request affecting four or more distinct Entries, so that I approve larger changes explicitly.
37. As the Watchlist owner, I want that threshold enforced across all tool calls within a request, so that splitting changes cannot bypass approval.
38. As the Watchlist owner, I want approval to authorize only the displayed actions and Entries, so that later discoveries cannot expand it silently.
39. As the Watchlist owner, I want approval to be single-use and expire after 30 minutes, so that an old approval cannot unexpectedly execute later.
40. As the Watchlist owner, I want successful additions preserved when others fail or need clarification, so that partial problems do not undo completed work.
41. As the Watchlist owner, I want a combined summary of added, already-present, awaiting-clarification, and failed results, so that I know what happened to every requested title.
42. As the Watchlist owner, I want retried Slack deliveries and restarted operations to avoid repeated completed writes, so that retries do not create duplicates or repeat actions.
43. As the Watchlist owner, I want extraction and search limits explained with an explicit continuation option, so that large tasks remain controlled without silent truncation.
44. As the project's maintainer, I want runnable tests of tool calls, approvals, and recovery, so that I can learn how to enforce and verify agent guardrails.

## Implementation Decisions

- **Agent harness:** Use LangChain `create_agent`; the agreed feature requires a conversational
  tool loop. Deep Agents and the two-framework comparison are deferred. No general filesystem
  or shell-execution capabilities are required.
- **Tool surface:** Provide Watchlist reads, identity resolution, add, soft-remove, mark
  watched/unwatched, refresh Enrichment, web search, and URL list extraction. Recommendation
  and discovery can compose read/search capabilities. Expose specific operations rather than
  unrestricted Notion writes or schema modification.
- **Existing ownership:** Watchlist tools call the existing Runtime for Enrichment. Preserve
  page-based checkpoints, Disambiguation interrupts and resumes, Enrichment Status, Reconcile
  coordination, provider rate limits, progress reporting, and late completion notifications.
  The conversational agent must not independently reproduce OMDb/RT enrichment steps.
- **Conversation identity:** Persist conversation context by workspace, channel, and thread
  identity. Persist the ordered Entry IDs behind displayed lists. Enrichment checkpoints
  remain keyed by Notion Entry identity, separate from the conversation.
- **Slack behavior:** A mention starts a conversation. Accept the configured owner's ordinary
  replies in an established bot thread. Ignore bot-generated messages and unrelated thread
  traffic. Enforce the configured owner at the request and interactive-action boundaries.
- **Request planning:** Prepare the complete action list before writes. Preserve one request
  identity across its tool calls, approval interaction, clarification, and recovery. Splitting
  a request into tool calls or subplans cannot reset its count. Explicit continuations are
  new user instructions and must pass the same policy.
- **Approval policy:** Count distinct affected Entries across the whole request, including
  proposed new Entries distinguished by resolved IMDb Identity. Four or more require
  approval before any writes. Mixed actions count together. Every URL Import requires its
  preview approval, which can also authorize the displayed bulk operation.
- **Approval binding:** Bind approval to the exact actions and Entries presented, the owner,
  and the request. It is single-use and expires after 30 minutes. Reject changed, expired,
  reused, or unauthorized approvals. Persist execution progress once approved; recovery of
  the same execution is not a second use of its approval. Unexpected additional targets
  pause execution and require approval of a revised plan.
- **Enforcement boundary:** Application code enforces approvals, permitted operations, target
  validity, and ownership before external writes. Prompts and model decisions alone cannot
  authorize a write. Read-only research and extraction do not authorize actions.
- **IMDb Identity:** Resolve identity before creating an Entry and deduplicate by IMDb ID,
  preserving distinct same-name works. Store resolved IMDb Identity in the Watchlist. Verify
  the live schema before adding the corresponding property. Existing unidentified Entries
  require conservative matching and clarification when uncertain; do not destructively merge
  existing data. Adapt the Runtime entry point as needed while preserving its page-based
  enrichment lifecycle.
- **Watched projection:** Read the existing Watched checkbox and support queries for unwatched
  Entries independently of the pending-Enrichment query. Keep viewing state separate from
  Enrichment Status. Missing or malformed viewing state is unknown rather than evidence
  that the Entry is unwatched. Tracking remains at Entry level, including TV shows.
- **Recommendations:** Default to up to five varied existing unwatched Entries, giving a
  short explanation based on stored genre, plot, and ratings. Return fewer when the eligible
  pool is smaller. Respect thread follow-ups and explicit preferences. Distinguish critic
  and audience scores and do not invent missing facts.
- **External evidence:** Search when required by a question or an evidence gap, with links
  supporting web-derived claims. External discovery is available on explicit request; label
  those suggestions and require an Add request before creating Entries. Search results and
  web pages are untrusted data and cannot amend instructions or authorize writes.
- **URL boundaries:** Extract the intended list from the supplied page, with a selectable
  preview. Validate retrieval URLs and redirects at the trust boundary, permitting public
  web content without exposing local/private network resources. Do not automatically follow
  other pages or bypass inaccessible content.
- **Initial ceilings:** Extract at most 25 titles from the supplied page and perform at most
  three web searches per request. State when a limit is reached and offer explicit
  continuation. These ceilings may be increased deliberately after observing behavior;
  continuation retains approval and trust-boundary requirements.
- **Write semantics:** Removal uses recoverable Notion soft deletion only. Mark operations
  change Watched. Refresh may replace machine-managed ratings, plot, and genre while
  preserving viewing state and Entry identity; identity changes require clarification.
- **Durable execution:** Persist request plans, approval state, displayed references, and
  per-action progress using the project's existing SQLite persistence where appropriate.
  Serialize competing execution for the same request/Entry and make retries idempotent.
  Reconcile uncertain external-write outcomes after a crash or timeout before repeating
  writes; a successful remote write without a local receipt must not create a duplicate.
- **Partial results:** Independent actions can succeed while others fail or await
  Disambiguation. Preserve completed additions and report each outcome in the original
  thread. No bulk rollback or deletion of successful additions is introduced.

## Testing Decisions

- **Confirmed primary seam:** Test complete Slack conversation requests through the existing
  transport-to-Runtime boundary, observing replies and resulting Watchlist changes. Prefer
  this single high-level seam over tests tied to internal graph nodes, prompts, or tool
  dispatch implementation.
- **Deterministic suite:** Use fixture-backed external services and a scripted agent that
  emits adversarial tool-call sequences. Use real durable state across Runtime restarts in
  recovery scenarios. This tests application enforcement independently of model compliance.
- **Prior art:** Extend the existing asynchronous unittest style and Slack transport tests,
  which already use fake Runtime/Notion/graph collaborators and verify threaded progress,
  duplicates, failures, and human Disambiguation routing.
- **Behavior under test:** Cover Slack routing and owner authorization, conversational
  references, Watchlist tool effects, request-wide approval, identity deduplication,
  Enrichment integration, URL/search boundaries, and persistent execution recovery through
  externally observable results.
- **Approval cases:** Three distinct affected Entries can proceed without bulk approval;
  four cannot. Several smaller tool calls and mixed actions still count together. URL
  Imports require approval even for one title. Reject expired, replayed, altered-plan,
  cross-request, and unauthorized approvals. No writes occur before required approval.
- **Identity cases:** Multi-title requests create separate Entries, compound titles are
  preserved, alias names deduplicate by IMDb Identity, remakes remain distinct, and uncertain
  matches clarify rather than guess. Exercise existing Entries lacking stored IMDb Identity.
- **Recovery cases:** Simulate duplicate Slack delivery, restart during approval, restart
  after partial execution, concurrent attempts, and failure between an external write and
  recording its receipt. Assert that completed effects are preserved without duplication.
- **Conversation cases:** A numbered reference acts on the displayed Entry after restart;
  separate threads remain isolated. Recheck removed or changed Entries before actions.
- **Research cases:** Untrusted page/search instructions cannot cause unauthorized tools.
  URL extraction excludes unrelated links; blocked or unreadable pages produce useful
  feedback. Extraction/search ceilings produce explicit notices. Retrieval cannot reach
  disallowed local/private resources through either an initial URL or a redirect.
- **Recommendation cases:** Exclude Watched Entries, respect preferences, return fewer than
  five when necessary, acknowledge missing metadata, cite web-derived evidence, and do not
  add external suggestions without a request.
- **Write cases:** Soft removal is recoverable; watched/unwatched updates preserve metadata;
  refresh preserves Watched and identity; partial failures preserve successful additions
  and expose per-Entry outcomes.
- **Model evaluation:** Run real model tool calls against the same fixture-backed tools for
  the agreed scenarios. Keep nondeterministic evaluation distinct from deterministic policy
  checks; it must demonstrate intent interpretation and useful responses without production
  writes. Compare expected outcomes rather than exact prose or internal call order.
- **Live verification:** After fixture-backed checks, use controlled Slack interactions and
  a dedicated Notion test database to verify actual API behavior, soft deletion, Enrichment,
  approval interactions, and restart durability. Keep production data outside test writes.
- **Runnable delivery:** Leave a documented command for deterministic tests and a separate
  opt-in command for model evaluation. Run existing formatting, lint, and type checks as
  well; the current static-check command does not itself execute tests. State any blocked
  model or live validation explicitly.

## Out of Scope

- Deep Agents adoption, a two-framework prototype, shell access, and general filesystem tools.
- Multiple users, separate viewing histories, persistent taste profiles, and autonomous
  long-term preference learning.
- Season-level or episode-level viewing state.
- Permanent deletion, automatic merging or deletion of existing duplicates, and rolling back
  successful additions when other batch actions fail.
- Arbitrary Notion writes, manual rating/plot editing, title/type editing, and agent-driven
  schema changes. The controlled IMDb Identity property change is part of implementation.
- Automatic additions from recommendations or unsolicited external discovery.
- Automatic crawling beyond the supplied page, unrestricted search, and bypassing inaccessible
  sites or managed access restrictions.
- Streaming availability claims without supporting evidence or unrelated Watchlist features.

## Further Notes

- This spec synthesizes the completed design interview and its glossary and architecture
  decisions. It does not authorize application implementation by itself.
- The primary testing seam was explicitly confirmed by the user during spec preparation.
- Watched is documented and present in a captured Notion fixture, but the live schema still
  needs verification. The current Entry projection omits it. Existing duplicate detection
  uses typed titles, and URL extraction and request-wide approvals are new capabilities.
- The existing Runtime creates a pending Notion Entry before Enrichment. Identity-based
  pre-create deduplication needs deliberate integration while retaining the existing
  checkpoint, Disambiguation, Reconcile, and notification contracts.
- ADR 0012's typed-title deduplication and create-before-resolution sequence are revised
  for conversational Adds: resolve and deduplicate first, then create the pending Entry
  before invoking page-based Enrichment. This does not move page creation to the end of
  Enrichment. Update the affected ADR during implementation to explain the changed boundary.
- Preserve ADR 0008's Judge guard against wrong identity and cross-lane mismatches. Carry
  the user's original request evidence into validation rather than treating an agent's
  proposed IMDb Identity as unquestionable ground truth.
- Published to the configured local Markdown tracker with `Status: ready-for-agent`.

## Implementation validation

The conversational manager, application-owned approvals, durable thread references and
execution receipts, bounded web research, and existing Enrichment integration are implemented.
Deterministic Slack-to-Runtime tests, installed-agent tool-loop tests, static checks, and
Compose configuration checks cover the implementation. Review findings were corrected.

Live activation remains pending: configure the agent model and owner, prepare the missing
IMDb ID property, and enable Slack thread message subscriptions as documented in README.md.
The existing Watched property was verified read-only. The configured model endpoint was
unreachable during validation, so real-model evaluation did not pass; controlled Slack/Notion
mutation tests were not run. No production Watchlist entries or schema were changed.

# Conversational Watchlist design

The design was confirmed and implemented in the conversational Watchlist manager. Product
and architecture decisions are recorded below; activation and live validation are documented
in README.md.

## Agreed boundaries

- The bot serves one user and their personal Watchlist; no separate users' viewing histories.
- Functionality drives framework choice; start with LangChain `create_agent`.
- Explicitly named movies or TV shows can be added without a preview when below the bulk
  approval threshold, retaining human Disambiguation when identity is uncertain.
- URL Import extracts the intended list and presents a selectable preview before creating
  Entries. Web-page content is data and cannot authorize additional actions.
- Recommendations default to existing unwatched Entries. Suggesting an Entry does not add it.
- The existing `Watched` checkbox is authoritative for recommendation eligibility.
- Default recommendations are five varied choices using stored genre, plot, and ratings.
  The agent has a web-search tool and may supplement stored information when required.
- Conversation context is retained within the same Slack thread.
- Batch operations preserve successful additions when other titles fail or need clarification.
- Initial Watchlist tools are add, remove, mark watched/unwatched, and refresh enrichment.
- Removal is soft deletion only. Requests affecting four or more distinct Entries require
  explicit approval.
- Web search runs when needed to answer a specific question or fill an evidence gap, with
  links to supporting sources.
- Conversation context and the Entry IDs behind displayed lists persist per Slack thread.
- Start with LangChain `create_agent`. The required capability is a conversational tool
  loop; Deep Agents' additional filesystem and execution capabilities are unnecessary for
  this scope. Revisit the harness if future requirements justify it.
- Approval counting spans the entire request, so splitting writes across tool calls cannot
  bypass the threshold. URL preview approval can authorize the displayed import batch.
  Approval is bound to the exact actions and Entries previewed, single-use, and expires
  after 30 minutes. Changes require a new preview. Execution progress persists so retries
  cannot repeat completed writes.
- Deduplicate by resolved IMDb identity; preserve distinct same-name movies or shows.
- The conversational agent invokes the existing enrichment Runtime through dedicated tools.
  Conversation state is keyed by Slack thread; enrichment state remains keyed by Notion
  Entry. The agent does not independently reproduce the enrichment workflow.
- Explicit external discovery may suggest titles outside the Watchlist. Default unwatched
  recommendations remain inside it; external suggestions require an Add request to create
  Entries.
- The two-framework comparison is dropped in favor of `create_agent`. Tool-call and
  guardrail testing remains an explicit learning goal. Validate with real model calls
  against fixture-backed tools, then controlled Slack/Notion integration checks.
- Initial ceilings are 25 extracted titles from the supplied URL, without automatically
  following other pages, and three web searches per request. Explain limits and offer an
  explicit continuation when reached.
- Require a mention to start a conversation and accept ordinary replies in that bot thread.
  Treat all user messages and interactive actions as the personal user; no Slack user
  allowlist is required.
- Prepare the complete action list before executing writes. Unexpected additional targets
  pause execution and require approval of a revised plan; no silent expansion is permitted.
- Refresh may replace machine-managed ratings, plot, and genre, while preserving watched
  state and Entry identity. A changed resolved identity requires clarification.

## Verified current capabilities

- Slack accepts `add` and `run`; the entire argument to `add` is currently one title.
- `Runtime.create_and_enrich` creates a pending Notion Entry before Enrichment. The Entry's
  page ID anchors checkpointing, human Disambiguation, and completion replies.
- The enrichment graph includes identity resolution, write-back, and notifications as well
  as OMDb and Rotten Tomatoes retrieval. Reusing the Runtime preserves those contracts.
- `Watched` exists as a checkbox in the documented schema and captured Notion fixture
  (`RESEARCH.md`, `tests/fixtures/notion_query.json`). The current Entry projection omits it;
  no unwatched query is implemented. Live schema verification remains outstanding.
- Duplicate detection compares typed titles, without canonical identity or request replay
  protection. Arbitrary URL list extraction and batch coordination are not implemented.

## Implementation and validation

Implement the tool boundary and durable request bookkeeping first, then integrate
`create_agent` and Slack conversation handling. Continue using the existing Runtime and
page-based enrichment checkpoints. Verify the live Notion schema before introducing an
IMDb identity property or reading watched state; existing unidentified Entries need
conservative matching and clarification when uncertain.

Leave runnable deterministic checks for request-wide approval enforcement, identity-based
deduplication, partial success, restart/retry recovery, user-message routing, and untrusted
page content. Exercise real model tool calls separately against fixture-backed tools to
measure behavior without making production writes. Use a dedicated Notion test database
for subsequent controlled integration checks.

Acceptance scenarios:

- Two named movies produce separate Entries; ambiguous identities receive clarification.
- Alias names resolve to one IMDb identity; same-name remakes remain distinct.
- A request affecting four Entries performs no writes before approval, including when the
  agent attempts several smaller tool calls.
- URL content cannot authorize tools; only the selected and approved import is executed.
- Expired, reused, or changed-plan approvals cannot execute writes.
- Restart/retry recovery preserves progress and does not repeat completed writes; reconcile
  uncertain external-write outcomes before retrying.
- Recommendations use unchecked Watched Entries and cite supplemental web evidence.
- Thread follow-ups retain displayed Entry IDs across restarts and recheck current state.
- Removal is recoverable, refresh preserves viewing state, and partial failures preserve
  successful additions.

Remaining step: user confirmation of this consolidated design before implementation.

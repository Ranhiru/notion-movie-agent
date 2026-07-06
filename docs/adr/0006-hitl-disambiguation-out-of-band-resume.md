# HITL title disambiguation: non-blocking interrupt + out-of-band Slack resume

When OMDb search returns multiple candidates for an Entry (e.g. *Dune* 1984/2000/2021),
an LLM disambiguation pre-filter attempts the pick first; a conditional edge escalates to
the human **only when the LLM is not confident** (see
[0008](./0008-llm-node-architecture.md)). On escalation the graph calls LangGraph
`interrupt()`. In implementation (Phase 6b) this lives in a **dedicated `await_human` node**
downstream of `disambiguate`, not inside `disambiguate` itself: `interrupt()` re-runs its
whole node from the top on resume, so isolating it keeps the disambiguation LLM (and the OMDb
`details` fetch, in `omdb_details`) from re-firing on every resume. The checkpointer
persists the graph state (keyed by `thread_id = page_id`), the Entry is set to a new
status **`awaiting_input`**, a Slack prompt with the candidates is posted, and the
reconcile **moves on to the next Entry**. When the human answers in Slack, the action
handler resumes the graph:
`graph.invoke(Command(resume=<chosen imdbID>), thread_id=page_id)` — **outside** the
reconcile sweep.

## When no candidate is correct: inline manual input, not a graph branch

OMDb `?s=` search sometimes never surfaces the right title in its top 5 (observed: *Michael*
→ `tt11378946`), so the picker cannot assume the answer is on the menu. The escape hatch is
an **inline `input` block** (`plain_text_input` with `dispatch_action`) in the same picker
message: the human pastes an IMDb link/id instead of clicking a candidate button. Both paths
deliver an imdbID to the *same* `Command(resume=…)` — the graph neither knows nor cares
whether the id came from a button, the input field, or the CLI (`--resume`).

The alternative — a "None of the above" button plus a `__none__` resume sentinel routed to a
manual-resolution branch in the graph — was **rejected**: it adds a second interrupt and a
conditional edge purely to model a conversation the Slack layer can hold on its own. With the
inline input the graph simply *stays paused at the original `interrupt()`* until a valid
imdbID arrives, so restart durability between "candidates are wrong" and "here's the link"
comes for free, and the resume contract stays a single type (always an imdbID).

## How this coexists with the single-flight lock

The resume **does not take the single-flight lock**. Three orthogonal mechanisms:

- **Single-flight lock** — serializes reconcile *sweeps* only (sweep-vs-sweep).
- **`enrichment_status`** — partitions rows by owning path: the sweep queries `pending`;
  a row awaiting/under resume is `awaiting_input`. A row is in exactly one status, so the
  sweep and a resume can never touch the same row (sweep-vs-resume).
- **Per-API rate limiters** (process-global) — the only thing the two paths share is
  external-API budget; a resume's calls queue behind a sweep's through the same limiter.

Because `interrupt()` makes `graph.invoke()` *return* (it doesn't block), the reconcile
loop naturally regains control and proceeds — "move on" is idiomatic, not a workaround.
Because `thread_id = page_id`, resuming an already-finished thread is a no-op, so Slack
double-clicks are safe.

## Consequences

- **The checkpointer becomes load-bearing, not a stretch goal**: graph state must survive
  a multi-hour human wait *and* a process restart → a durable checkpointer (SQLite on a
  volume, or Postgres), never `MemorySaver`.
- Detecting ambiguity requires OMDb **search** (`?s=`) to get the candidate list, not
  `?t=` (single best match).
- **Stale `awaiting_input`**: if the human never clicks within **7 days**, the hourly
  cron auto-resolves it using the disambiguation pre-filter's stored best-guess Candidate
  and marks it `done` with `confidence: low` (flagged for review). The best guess is
  stashed in the interrupt payload so the resume needs no recomputation. No Entry is ever
  stuck, and none is lost.
- Adds one status (`awaiting_input`) and one endpoint (Slack callback); everything else
  (graph, checkpointer, limiters, lock) is reused.
- **The resume value is always an imdbID** — candidate button, manual input field, 7-day
  auto-resolve, and CLI `--resume` all speak the same contract, so `await_human →
  omdb_details` stays the only edge out of the interrupt.

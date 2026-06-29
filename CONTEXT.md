# Notion Movie/TV Enrichment Agent

A service that watches a single Notion movie/TV database and fills in each entry's
IMDb rating, Rotten Tomatoes score, and plot summary from external sources.

## Language

**Entry**:
A single movie or TV-show in the Watchlist — one row in the Notion database / one Notion
page. Its title (the name) comes from the Notion "Title" property, surfaced as `entry.title`.
The entity is an *Entry*, so a variable is `entry` — never `title`, which would collide with
the `.title` field (the original `title.title` confusion).
_Avoid_: title/Title for the whole entity (that's the `.title` field), row, record, page, item

**Enrichment**:
The act of filling an Entry's missing fields (IMDb rating, Rotten Tomatoes score, plot)
from external sources and writing them back.
_Avoid_: scraping, fetching, updating

**Reconcile**:
The single entrypoint operation: find every Entry still needing enrichment and run
each through the enrichment graph. Triggered by both the webhook and the cron; runs
under a single-flight lock so only one is ever in progress.
_Avoid_: backfill, batch job, sync

**Straggler**:
An Entry left un-enriched after a reconcile — e.g. added while a reconcile was already
running, so its webhook was dropped. Picked up by the next reconcile or the hourly cron.
_Avoid_: leftover, missed row

**Origin**:
Where an Entry entered the system — `sweep` (a row added in Notion, discovered by reconcile)
or `slack` (typed via the `/add` command). Both produce the same pending Entry and run the
same enrichment; Origin only governs whether the run reports its result back to Slack on
completion.
_Avoid_: source (means a Provider/Lane here), trigger

**Add**:
Originating an Entry from Slack via the `/add <name>` command: create the Notion page the
user would have typed, then enrich it. A second entry point alongside the Notion row, not a
replacement.
_Avoid_: import, create row, slash command (the mechanism, not the act)

**Lane**:
One of the two source strategies feeding the merge step, run in parallel:
the **metadata lane** (OMDb → IMDb rating, IMDb ID, plot) and the **RT resolution**
lane. Not interchangeable APIs — distinct strategies.
_Avoid_: source, provider, API

**RT Resolution**:
The lane that obtains Rotten Tomatoes scores via an ordered fallback chain, active by
default: `Firecrawl /search → Tavily → Exa → Perplexity`. Captures two best-effort
scores — the **critic** score (Tomatometer) and the **audience** score (Popcornmeter).
The first provider to return at least one score wins; the rest don't run. Modelled as a
subgraph.
_Avoid_: scraping, the search lane

**Critic Score / Audience Score**:
The two distinct Rotten Tomatoes numbers. **Critic** = Tomatometer (professional
reviews). **Audience** = Popcornmeter (user ratings). Stored in separate fields; they
are often different numbers and must never be conflated.
_Avoid_: RT score (ambiguous), Tomatometer/Popcornmeter in field names

**Provider**:
One link in the RT Resolution chain (Firecrawl, Tavily, Exa, or Perplexity). Each is
tried in order until one yields a usable RT critic score.
_Avoid_: source, lane, API

**Enrichment Status**:
An Entry's lifecycle marker — `pending`, `awaiting_input`, `done`, or `failed`.
`done` = IMDb resolved (rating + plot) and the RT cascade exhausted (RT best-effort, may
be null). `failed` = OMDb definitively couldn't resolve the title. `awaiting_input` = the
graph is interrupted, waiting on a human Slack pick to disambiguate. `pending` = new, or
a run that didn't complete (crash / transient error) — left pending so the cron retries.
The reconcile query picks up only `pending` (or unset) Entries; status is also what keeps
the sweep and an out-of-band resume from ever touching the same Entry.
_Avoid_: state, flag

**Disambiguation**:
Choosing the right Candidate when OMDb search returns several for an Entry. An LLM
pre-filter attempts the pick first; a conditional edge routes on its confidence — when
confident it proceeds automatically, when unsure it interrupts and escalates to a human
via Slack (the HITL path), then resumes. Modelled with `add_conditional_edges` +
`interrupt()` + checkpointer.
_Avoid_: matching, resolution

**Judge**:
The LLM-as-judge fan-in node. Inspects the assembled record (OMDb fields + winning RT
scores) for wrong-match anomalies (e.g. IMDb 9.1 vs RT 18%, or a plot that doesn't fit
the title/year) and emits the `confidence` value. The system's guard against
confidently enriching the wrong Entry.
_Avoid_: validator, merge, reconciler

**Candidate**:
One possible OMDb match for a typed Entry (a specific title + year + type + imdbID)
surfaced during Disambiguation. The human picks one Candidate; its imdbID drives the
rest of enrichment.
_Avoid_: result, option, match

**Core**:
The required enrichment an Entry must have to be `done`: IMDb rating + plot (from OMDb).
Distinct from the best-effort RT scores. "Core failure" = OMDb can't resolve the title.
_Avoid_: required fields, base data

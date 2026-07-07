# RT resolution is a rotated-start provider chain, advancing on hard or soft failure

The Rotten Tomatoes critic score is obtained by a chain of three search providers —
**Firecrawl `/search` (scoped to rottentomatoes.com, with content scrape), Tavily, Exa** —
implemented as an injected **`SearchClient` strategy** behind the RT subgraph's single
`rt_search` node, and **active by default**. The chain's *order rotates per Entry*
(round-robin on a process-local counter): each Entry gets a different provider first, and
the other two remain fallbacks in rotation order. The first provider to return a usable
score wins; the rest don't run.

**Why rotation (amended decision):** the original design was a fixed order (Firecrawl first,
always), which concentrates ~all load on one provider. All three providers are used on
**free API tiers**, so the primary goal is distributing quota consumption across them; the
happy path touches exactly one provider per Entry either way, rotation just changes *which*
one. Perplexity, originally the fourth link, is **dropped** — three rotating providers
already give redundancy, and it's one less API key.

**Why a client-layer strategy, not sibling nodes (amended decision):** the original plan
grew the chain as peer provider *nodes* (`firecrawl_provider` / `tavily_provider` /
`exa_provider`) plus conditional edges. Superseded: the subgraph keeps a **single
`rt_search` node** (named for what it does, parallel to `omdb_search`), which takes any
`SearchClient` — a Protocol whose one method is
`search_rt_candidates(title, media_type) -> list[RTHit]`. A single provider client
(Firecrawl, Tavily, Exa) or a round-robin composite of them satisfies it identically, so
providers swap — and the chain grows — without touching graph topology. Two costs
accepted: per-provider chain steps are no longer separate LangSmith spans (the composite
logs the winning provider instead), and a soft miss can no longer be judged by extraction
outcome (see the redefinition below).

RT is captured as **two best-effort fields** — `rt_critic` (Tomatometer) and
`rt_audience` (Popcornmeter) — each a nullable 0–100. Neither is required; we store
whatever a provider yields. A provider "succeeds" if it surfaces **at least one canonical
RT candidate page** (amended — see below). The chain advances to the next provider on
**either**:

- **hard failure** — timeout, 5xx, exhausted rate limit, exception; or
- **soft miss** — the search succeeded but surfaced **no canonical RT candidate**
  (irrelevant results, no `/m/` or `/tv/` page).

**Soft miss is judged at the search layer (amended):** the original definition — "yielded
neither score" — required knowing the *extraction* outcome, but score extraction is an LLM
call that runs in the downstream `extract` node (and, for ambiguous sets, the parent's
`resolve_rt`), after the `SearchClient` has already returned. The chain therefore advances
on what search can see: errors and empty candidate lists. A page that is *found* but later
yields no score ends the lane with null RT rather than advancing the chain — an accepted
cost, because all three providers search the same site: a scoreless canonical RT page
would be re-found by a fallback, not fixed.

Transient errors are retried *within* a provider before falling through, so a single 429
doesn't burn a fallback. The chain short-circuits at the first provider surfacing a
candidate; if all three come up empty, both RT fields stay `null`
(best-effort — this does not, by itself, keep an Entry from being `done`).

Rotation assumes **provider parity**: each provider client must deliver the same
`SearchClient` contract (RT page discovery + inline content for score extraction + the
candidate shaping below — markdown-bearing `RTHit`s). If a provider proves consistently
weaker, remove or reorder it via `SEARCH_PROVIDERS` config — rotation walks whatever list
that yields. The round-robin counter is in-process and unpersisted; distribution being
approximate across restarts is fine (the goal is spreading load, not exact fairness).

Alongside the two scores, the winning provider also surfaces the **matched page's
reference** — the canonical RT URL / title (and year, when shown) — into **graph state**.
These are *not* stored in Notion (§8 is unchanged); they exist so the LLM-as-judge
([0008](./0008-llm-node-architecture.md)) can confirm the RT page is the same title OMDb
resolved. Because the RT lane discovers its page by title alone (it shares no key with the
OMDb lane), this reference is the only thing that lets a downstream check detect a
cross-lane wrong-match.

Within a single provider, how the matched page is chosen is itself a choice (see options
below): the base design picks **one** page deterministically; an extension surfaces a
**candidate set** for the Judge to correlate against OMDb.

## Considered Options

- **Off by default / Firecrawl-only (rejected, reverses RESEARCH.md §4):** minimal keys
  and cost, but no resilience when Firecrawl misses an RT score.
- **Advance only on hard failure (rejected):** a soft miss would stop the chain with an
  empty RT and the fallbacks would never run — defeating the point of building them.
- **Fixed-order chain, Firecrawl always first (superseded):** the original decision.
  Resilient, but funnels ~all quota to Firecrawl's free tier while Tavily/Exa idle.
- **Fan-out all providers per Entry, first score wins (rejected):** fastest and most
  resilient, but burns ~3× quota on every Entry — directly against the free-tier goal.
- **Single provider per Entry, no fallback (rejected):** lowest quota use, but one soft
  miss leaves the Entry's RT null until a manual re-run — gives up the chain's resilience.
- **Rotated-start chain, advance on hard-or-soft (chosen).**
- **Chain as sibling provider nodes + conditional edges (superseded):** the original
  implementation shape. Makes each chain step a LangSmith span, but grows graph topology
  with every provider and hard-codes rotation into edge functions. Superseded by the
  injected `SearchClient` strategy above.
- **Extraction-outcome soft miss (superseded):** advancing the chain when a found page
  yields no *score* would require the LLM extraction inside the search strategy (entangles
  the layers, breaks the deferred >1-candidate path) or a feedback edge from `extract`
  back to search (chain logic back in the graph). Rejected with the client-layer choice.

### Per-provider page selection

- **Deterministic single pick (base):** `rank_rt_hits()[0]` chooses the canonical
  `/m/` or `/tv/` page, biased by `media_type`. Cheap, no LLM; right for the common case
  where one RT page obviously matches. A "soft miss" = that page yielded no score.
- **Candidate set + Judge correlation (adopted in Phase 5c as hybrid escalation, see
  [0008](./0008-llm-node-architecture.md)):** when **>1 canonical RT page is in
  contention**, the provider surfaces the ranked candidates
  `{rt_url, rt_title, rt_year, rt_markdown}` and defers extraction; the post-fan-in
  `resolve_rt` node correlates them against OMDb's resolved title/year/plot and extracts
  scores once from the winner. The inline-scraped markdown for the extra hits is already
  paid for; metadata-first correlation keeps score extraction to one call. Here a "soft
  miss" generalizes to "no candidate the correlation accepts."

## Consequences

- v1 requires three search-provider keys (Firecrawl, Tavily, Exa) plus OMDb and the LLM
  roles — each individually stays within its free tier because rotation splits the
  ~hourly sweep's load three ways.
- Every provider client must be a full `SearchClient` peer (search + content + candidate
  shaping), not a thin "try this URL" fallback — parity is what makes rotation safe.
- The subgraph's search node is provider-agnostic (`rt_search`, mirroring `omdb_search`);
  which provider actually ran is visible via the composite's log line / surfaced provider
  name, not via per-node LangSmith spans.

# RT resolution is a rotated-start provider chain, advancing on hard or soft failure

The Rotten Tomatoes critic score is obtained by a chain of three search providers —
**Firecrawl `/search` (scoped to rottentomatoes.com, with content scrape), Tavily, Exa** —
modelled as a subgraph and **active by default**. The chain's *order rotates per Entry*
(round-robin on a process-local counter): each Entry gets a different provider first, and
the other two remain fallbacks in rotation order. The first provider to return a usable
score wins; the rest don't run.

**Why rotation (amended decision):** the original design was a fixed order (Firecrawl first,
always), which concentrates ~all load on one provider. All three providers are used on
**free API tiers**, so the primary goal is distributing quota consumption across them; the
happy path touches exactly one provider per Entry either way, rotation just changes *which*
one. Perplexity, originally the fourth link, is **dropped** — three rotating providers
already give redundancy, and it's one less API key.

RT is captured as **two best-effort fields** — `rt_critic` (Tomatometer) and
`rt_audience` (Popcornmeter) — each a nullable 0–100. Neither is required; we store
whatever a provider yields. A provider "succeeds" if it returns **at least one** of the
two scores. The chain advances to the next provider on **either**:

- **hard failure** — timeout, 5xx, exhausted rate limit, exception; or
- **soft miss** — the call succeeded but yielded **neither** score (no meters on the
  page, irrelevant results).

Transient errors are retried *within* a provider (via `RetryPolicy`) before falling
through, so a single 429 doesn't burn a fallback. The chain short-circuits at the first
provider returning any score; if all three come up empty, both RT fields stay `null`
(best-effort — this does not, by itself, keep an Entry from being `done`).

Rotation assumes **provider parity**: each provider node must deliver the same contract
(RT page discovery + content for score extraction + the candidate shaping below). If a
provider proves consistently weaker, remove or reorder it via `SEARCH_PROVIDERS` config —
rotation walks whatever list that yields. The round-robin counter is in-process and
unpersisted; distribution being approximate across restarts is fine (the goal is spreading
load, not exact fairness).

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
- Every provider node must be a full peer (search + content + candidate shaping), not a
  thin "try this URL" fallback — parity is what makes rotation safe.

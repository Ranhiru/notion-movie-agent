# RT resolution is an ordered fallback chain, advancing on hard or soft failure

The Rotten Tomatoes critic score is obtained by an ordered chain —
`Firecrawl /search (scoped to rottentomatoes.com, with content scrape) → Tavily →
Exa → Perplexity` — modelled as a subgraph and **active by default**. The first
provider to return a usable score wins; the rest don't run.

RT is captured as **two best-effort fields** — `rt_critic` (Tomatometer) and
`rt_audience` (Popcornmeter) — each a nullable 0–100. Neither is required; we store
whatever a provider yields. A provider "succeeds" if it returns **at least one** of the
two scores. The chain advances to the next provider on **either**:

- **hard failure** — timeout, 5xx, exhausted rate limit, exception; or
- **soft miss** — the call succeeded but yielded **neither** score (no meters on the
  page, irrelevant results).

Transient errors are retried *within* a provider (via `RetryPolicy`) before falling
through, so a single 429 doesn't burn a fallback. The chain short-circuits at the first
provider returning any score; if all four come up empty, both RT fields stay `null`
(best-effort — this does not, by itself, keep an Entry from being `done`).

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
- **Full chain, advance on hard-or-soft (chosen).**

### Per-provider page selection (Phase 5+, open)

- **Deterministic single pick (current / base):** `pick_rt_hit` chooses the canonical
  `/m/` or `/tv/` page, biased by `media_type`. Cheap, no LLM; right for the common case
  where one RT page obviously matches. A "soft miss" = that page yielded no score.
- **Candidate set + Judge correlation (extension, see
  [0008](./0008-llm-node-architecture.md)):** the provider surfaces the top-N RT title
  pages as candidates `{rt_url, rt_title, rt_year, rt_critic, rt_audience}`; the Judge
  picks the one matching OMDb's resolved title/year. The inline-scraped markdown for the
  extra hits is already paid for; metadata-first correlation keeps score extraction to one
  call. Recommended only as **escalation when >1 canonical RT page is in contention**
  (mirrors OMDb's 0/1/many). Here a "soft miss" generalizes to "no candidate the Judge
  accepts." Recorded as an option, not yet committed.

## Consequences

- v1 requires four search-related keys (Firecrawl, Tavily, Exa, Perplexity) plus OMDb
  and the merge LLM — more quota/cost than the minimal path, accepted for resilience.

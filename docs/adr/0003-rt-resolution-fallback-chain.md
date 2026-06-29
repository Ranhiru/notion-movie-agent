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

## Considered Options

- **Off by default / Firecrawl-only (rejected, reverses RESEARCH.md §4):** minimal keys
  and cost, but no resilience when Firecrawl misses an RT score.
- **Advance only on hard failure (rejected):** a soft miss would stop the chain with an
  empty RT and the fallbacks would never run — defeating the point of building them.
- **Full chain, advance on hard-or-soft (chosen).**

## Consequences

- v1 requires four search-related keys (Firecrawl, Tavily, Exa, Perplexity) plus OMDb
  and the merge LLM — more quota/cost than the minimal path, accepted for resilience.

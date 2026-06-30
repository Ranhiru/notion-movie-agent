# LLM nodes: per-provider extraction, disambiguation pre-filter, LLM-as-judge

The original "LLM merge node that reconciles three conflicting sources" (RESEARCH.md) is
replaced. After the redesign there are two lanes, OMDb is authoritative structured JSON,
and the RT chain yields a single winner — so there is little to *reconcile*. The
`with_structured_output` LLM work is redistributed to where it does real work. The endpoint
and per-role models are set by [0011](./0011-openrouter-llm-provider.md) (configurable
OpenAI-compatible base URL — OpenRouter or a local LLM — with one configurable model each for
extraction / disambiguation / judge; every model must support structured outputs).

Three LLM roles:

1. **Per-provider RT extraction.** Each RT provider node runs `with_structured_output`
   to parse its raw output (Firecrawl markdown / search answer) into `{rt_critic,
   rt_audience}`. This is also what drives the fall-through in
   [0003](./0003-rt-resolution-fallback-chain.md): ≥1 score = success, else advance.
   Extraction must live here, not at the fan-in, or the chain can't tell a hit from a
   soft miss. The node also surfaces the **matched page's identity** — the canonical RT
   URL / title (and year, when shown) — into graph state alongside the scores; the Judge
   (role 3) needs it to confirm the RT page is the *same title* OMDb resolved.

2. **LLM disambiguation pre-filter + conditional routing.** When OMDb search returns
   multiple Candidates, an LLM picks the most likely one. An `add_conditional_edges`
   route then branches on its confidence: confident → proceed automatically; unsure →
   `interrupt()` and escalate to the human via Slack (see
   [0006](./0006-hitl-disambiguation-out-of-band-resume.md)). The human is pinged only
   for the genuinely ambiguous tail.

3. **LLM-as-judge fan-in.** The fan-in node inspects the assembled record for wrong-match
   anomalies and emits the `confidence` field. This is the only guard against confidently
   enriching the *wrong* Entry. Crucially, the assembled record must carry the **identity of
   both lanes** — OMDb's title / year / `imdbID` *and* the winning RT page's title / year /
   URL — not just the numbers. The two lanes resolve identity **independently, with no shared
   key** (OMDb by title → `imdbID`; RT by title → an RT slug), so they can silently land on
   different titles (e.g. *Orphan Black* 2013 vs *Orphan Black: Echoes* 2024). Bare scores
   only expose gross score-implausibility (IMDb 9.1 vs RT 18%); **title-level** matching needs
   both identities present. The Judge is therefore a *heuristic* backstop, not a deterministic
   join — it catches mismatches it can see in the assembled context. `confidence` ∈ {high,
   medium, low} is **trace-only** (LangSmith / graph state); it is *not* a §8 Notion property.

## Extension: RT candidate-set correlation (Phase 5+, not yet decided)

The base design (role 1) lets the RT lane deterministically pick **one** page via
`pick_rt_hit` (canonical `/m/` vs `/tv/`, biased by `media_type`) and surface that single
match. An optional extension makes the RT lane **candidate-shaped**, symmetric with OMDb
(which is already a candidate list per [0006](./0006-hitl-disambiguation-out-of-band-resume.md)):
instead of pre-picking, the lane surfaces the **top-N RT title pages** as candidates — each
`{rt_url, rt_title, rt_year, rt_critic, rt_audience}` — and the Judge **correlates** them
against OMDb's resolved identity to choose the right one (or flag that none match).

- **Cheap where it counts:** Firecrawl `/search` already scrapes every hit inline, so the
  markdown for multiple RT pages is *already paid for* — today `pick_rt_hit` discards all but
  one. The only added cost is LLM extraction.
- **Cost control (metadata-first):** the Judge picks the matching page by **title/year**
  (cheap search-hit metadata) *before* score extraction, so scores are extracted **once**,
  from the winner — not once per candidate.
- **Hybrid escalation (mirror OMDb's 0/1/many):** keep the deterministic `pick_rt_hit` fast
  path for the common **single canonical match**; only surface a candidate *set* to the Judge
  when **>1** canonical RT page is in contention. Complexity/LLM cost is paid only for the
  ambiguous tail (e.g. *Orphan Black* vs *Orphan Black: Echoes*).
- **Role overlap:** this grows the Judge from "anomaly detector" into an **RT disambiguator**,
  conceptually the RT-side twin of role 2's OMDb pre-filter. Decide whether that lives in the
  Judge or a dedicated RT-disambiguation node.
- **Ordering coupling:** correlation needs OMDb's *resolved* identity, so a still-ambiguous
  OMDb result (multi-candidate, deferred to Phase 6a) must resolve first. Still expressible at
  the fan-in, but it is a soft cross-lane dependency the base parallel design doesn't have.

Recorded as an option, not a commitment: the simpler title-match Judge (role 3) may be enough.
If adopted, "soft miss" in [0003](./0003-rt-resolution-fallback-chain.md) generalizes from
"no score" to "no candidate the Judge accepts."

## Consequences

- Exercises four LangGraph patterns: structured extraction, conditional edges
  (`add_conditional_edges` — previously absent from the design), HITL `interrupt()`, and
  LLM-as-judge.
- The fan-in is no longer a "reconcile" step; deterministic assembly (copy OMDb fields +
  identity + the winning RT scores **and that page's identity**) feeds the Judge, which
  supplies `confidence`. The lanes write disjoint state channels, so assembly is just the
  merged state — but the RT lane must propagate its page reference up, not discard it.
- A confidently-wrong OMDb match — or a *cross-lane* mismatch where RT resolved a different
  title than OMDb — is caught by the Judge (low confidence / flag), not silently written.

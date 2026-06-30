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

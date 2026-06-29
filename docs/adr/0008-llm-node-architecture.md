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
   soft miss.

2. **LLM disambiguation pre-filter + conditional routing.** When OMDb search returns
   multiple Candidates, an LLM picks the most likely one. An `add_conditional_edges`
   route then branches on its confidence: confident → proceed automatically; unsure →
   `interrupt()` and escalate to the human via Slack (see
   [0006](./0006-hitl-disambiguation-out-of-band-resume.md)). The human is pinged only
   for the genuinely ambiguous tail.

3. **LLM-as-judge fan-in.** The fan-in node inspects the assembled record (OMDb fields +
   winning RT) for wrong-match anomalies and emits the `confidence` field. This is the
   only guard against confidently enriching the *wrong* Entry.

## Consequences

- Exercises four LangGraph patterns: structured extraction, conditional edges
  (`add_conditional_edges` — previously absent from the design), HITL `interrupt()`, and
  LLM-as-judge.
- The fan-in is no longer a "reconcile" step; deterministic assembly (copy OMDb fields +
  take winning RT) feeds the Judge, which supplies `confidence`.
- A confidently-wrong OMDb match is caught by the Judge (low confidence / flag), not
  silently written.

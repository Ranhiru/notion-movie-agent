# TASKS — Notion Movie/TV Enrichment Agent

Phased, thin-slice implementation checklist. Architecture is locked in `CONTEXT.md`,
`RESEARCH.md`, and `docs/adr/0001–0011`. Check items off as we go.

## Context

`notion-db-updater` is a **fully-designed, zero-code greenfield** project. The goal is a
Python + **LangGraph** service that enriches a Notion "Watchlist" database (IMDb rating, RT
critic + audience scores, plot, genre). It is explicitly a **learning project** for LangGraph
primitives (fan-out/fan-in, `with_structured_output`, conditional edges, subgraphs,
`interrupt()` + checkpointer HITL, LLM-as-judge), built local-first and deployed via
`docker compose`.

This plan decomposes the work into **thin, independently-verifiable slices**. It adopts an
optimized ordering over RESEARCH §11's literal sequence **while staying compliant with every
ADR**:
- **RT lane before the Judge** — the Judge's job is catching IMDb-vs-RT anomalies; it needs a
  full record to inspect (ADR 0008). Building it before RT yields a degenerate node.
- **HITL split into 6a–6d** — ADR 0006's bundle (checkpointer + candidate path + pre-filter +
  `interrupt()` + Slack + 7-day timeout) is 4+ independently-failable slices.
- **Spike phase 0** — de-risk the recent/uncertain APIs (LangGraph 1.x, Notion 2025-09-03
  data_sources, `AsyncSqliteSaver`+`interrupt()` restart, Slack Socket Mode, **local-LLM
  structured output**) before they block a feature phase.
- **Docker deferred to the last phase** — ADR 0009 makes Docker a *deploy* concern; the early
  build is "just a long-lived process." Deferring it is *more* ADR-aligned, not less.

**Decisions of record (confirmed):**
- **Ordering:** optimized order; must still adhere to `docs/adr/*`.
- **Dev LLM:** **local LLM** (OpenAI-compatible endpoint per ADR 0011) → a local-model
  structured-output reliability spike is mandatory before any `with_structured_output` node.
- **Testing:** **live happy-path + recorded fixtures** for the tricky branches (soft-miss,
  0-result, multi-candidate, wrong-match judge). No full pytest-from-day-one mandate.
- **Confidence:** **trace-only** (LangSmith / graph state). No new Notion property; §8 model
  stays intact.

---

## Conventions (apply throughout)

- **Package manager:** `uv`. Pin LangGraph/LangChain **1.x** in `pyproject.toml`; pin exact
  versions early (spikes verify against the *installed* version, not memory/tutorials).
- **Naming:** use the ubiquitous language from `CONTEXT.md` for nodes/state/status —
  `Entry`, `Enrichment`, `Reconcile`, `Lane`, `Provider`, `Candidate`, `Judge`,
  `Enrichment Status` ∈ {`pending`, `awaiting_input`, `done`, `failed`}.
- **Notion:** API version `2025-09-03`; data source `Watchlist`
  id `ffcdcd68-0449-461d-be8e-0af9b71f9d5f`; query `POST /v1/data_sources/{id}/query`;
  token env `NOTION_MOVIE_DB_TOKEN`. **Idempotent upsert by `page_id`** everywhere.
- **LLM:** `ChatOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, model=…)` with three
  role models (`OPENAI_EXTRACTION_MODEL`, `OPENAI_DISAMBIGUATION_MODEL`, `OPENAI_JUDGE_MODEL`).
  Every LLM node uses `with_structured_output` → each model must support tool-calling.
- **LangSmith ON from phase 1** (pure config) — tracing is the primary way to *understand* the
  graph while learning. `LANGSMITH_TRACING=true`, APAC endpoint, project `NotionMovieDBAgent`.
- **Secrets:** `.env` in dev (never committed); ship `.env.example` with empty keys. Docker
  Compose file-mounted secrets only in phase 10.
- **Fixtures:** when a phase touches a tricky branch, capture a real response into
  `tests/fixtures/` (Notion query JSON, OMDb JSON, Firecrawl markdown) for offline tests.

---

## Phase 0 — Spikes (throwaway, timeboxed)

Goal: de-risk the recent/uncertain APIs before they block a feature phase. Throwaway code in
`spikes/`; the *learnings* (version-correct snippets) carry forward. Pin versions here.

- [x] `uv` project bootstrap with LangGraph/LangChain **1.x** pinned; confirm Python version.
      → Python 3.13.11; langgraph 1.2.6, langchain 1.3.11, langchain-openai 1.3.3,
      langgraph-checkpoint-sqlite 3.1.0. Exact pins in `pyproject.toml`.
- [x] **Notion 2025-09-03 spike:** one real `data_sources` query + one real write against the
      live Watchlist. Verify the query/filter body, the `is_empty OR equals pending` compound
      filter on a *select* property, and the write payload shape for `number` / `rich_text` /
      `select`. (De-risks phases 1, 2, 3 at once.)
      → `spikes/01_notion_data_source.py` **PROVEN live** (query: 200, compound filter works, all
      6 §8 property names verified; write: 200, number/rich_text/select round-trip confirmed).
      **Note:** Watchlist has blank rows (empty Entry) that match `is_empty` — Phase 3 resolution
      must skip/`failed` a blank Entry, not search OMDb for "". Lioness row holds `[spike]` test
      values (Phase 2 will overwrite).
- [x] **LangGraph 1.x toy graph spike:** 3-node graph exercising parallel fan-out + a reducer
      fan-in + one `add_conditional_edges` + one `interrupt()`, on the installed 1.x. Capture
      the exact `StateGraph` / `RetryPolicy` / `Command(resume=…)` signatures.
      → `spikes/02_langgraph_toy_graph.py` **PROVEN** (all assertions pass). Signatures in
      `spikes/README.md`. `RetryPolicy` is a NamedTuple; `interrupt()` makes `invoke()` return.
- [x] **`AsyncSqliteSaver` + `interrupt()` restart spike:** interrupt → **kill process** →
      restart → `graph.invoke(Command(resume=…), thread_id=…)` resumes. Confirm `interrupt()`
      makes `invoke()` *return* (ADR 0006 depends on this for "move on") and WAL/`.setup()`
      lifecycle is correct under `max_concurrency`. (Core learning target — biggest risk.)
      → `spikes/03_sqlite_interrupt_restart.py` **PROVEN across a real process restart**
      (interrupt in process A → exit → resume in fresh process B). journal_mode=WAL confirmed.
- [x] **Local-LLM structured-output spike:** run `with_structured_output` against the chosen
      local `OPENAI_BASE_URL`/model; confirm clean Pydantic parsing. If unreliable, surface
      now (fallback: OpenRouter for a known tool-calling model — two env vars).
      → `spikes/04_local_llm_structured_output.py` **PROVEN live**: all 3 role-models parse
      cleanly via `Qwen3.6-35B-A3B-MTP-GGUF` @ localhost:8888. **No OpenRouter fallback needed.**
      (All 3 roles point at one local model in dev — fine; config allows per-role swap later.)
- [x] **Firecrawl → RT extraction spike:** scrape 3–4 known titles' rottentomatoes.com pages;
      eyeball markdown; confirm an LLM can extract Tomatometer + Popcornmeter.
      → `spikes/05_firecrawl_rt_extraction.py` **PROVEN live, 4/4** (Dune 92/95, Godfather 97/98,
      Parasite 99/90, Last of Us 94/62). Two modes: `search` (default — the ADR-0003 path:
      title → Firecrawl `/search` → pick canonical RT URL → scrape inline → extract) and `scrape`
      (known URL, isolates extraction). **`search` proven 4/4**: each title resolved to the right
      canonical page (`/m/` vs `/tv/`, year suffix `parasite_2019`) from the title alone. Uses
      `maxAge`=1 week so repeat runs hit Firecrawl's cache. **Learnings:** (1) RT scores can sit
      ~15k chars into the markdown — blind `[:8000]` truncation dropped them; Phase 4 must slice
      smartly / watch the local model's context. (2) RT *TV* pages carry per-season scores → a
      critic/audience gap like 94 vs 62 is the kind of anomaly the Phase 5 Judge catches.
- [x] **Slack Socket Mode spike** (parallelizable): Bolt app posts a Block Kit button, logs the
      click payload. Confirms `xoxb-` + `xapp-`/`connections:write` + Interactivity wiring.
      → `spikes/06_slack_socket_mode.py` **PROVEN live**: bot posted picker, click delivered over
      the `xapp-` socket, `value` decoded to `{page_id, imdb_id}` (Phase 6c's `Command(resume=)`).
      **Note:** bot must be invited to the target channel (`/invite @bot`) — else `not_in_channel`.

**Verification:** each spike prints/proves its happy path; versions pinned in `pyproject.toml`.
→ Credential-free spikes (02, 03) + bootstrap proven by Claude. The 4 credential-gated spikes
are written, import cleanly, and self-guard on missing env vars — **run them with real creds**
(see `spikes/README.md`). The secrets aren't in Claude's shell env, so these are yours to run.

---

## Phase 1 — Skeleton + Notion read (no Docker)

Goal: a runnable `uv` project that reads the Watchlist. ADR 0009 (Docker is deploy-only).

- [x] Project layout: `pyproject.toml`, `src/` package, `.env` + `.env.example` (all keys from
      RESEARCH §10), `.gitignore`. LangSmith env wired and ON.
      → `src/notion_db_updater/` package; `pydantic-settings==2.14.2` added; hatchling
      build-system so `python -m notion_db_updater` resolves. `.env`/`.env.example`/`.gitignore`
      already correct from Phase 0.
- [x] Config loader: read env into a typed settings object (Notion token, OMDb, Firecrawl,
      OPENAI_* base/key/models, Slack tokens, LangSmith, `SEARCH_PROVIDERS`).
      → `config.py` `Settings(BaseSettings)` + cached `get_settings()`. Only
      `NOTION_MOVIE_DB_TOKEN` required; rest defaulted so the app boots pre-keys. LangSmith
      block present + `LANGSMITH_TRACING=true` default = tracing ON (nothing to trace yet).
- [x] Notion client module: `query_titles()` + `get_title(page_id)` against `2025-09-03`,
      honoring the spiked query shape; map property names from §8.
      → `notion.py` async `NotionClient` (httpx.AsyncClient, async-first per decision); spiked
      `is_empty OR equals pending` filter; `query_titles()` paginates `has_more`. `models.py`
      `Entry.from_page()` maps all 8 §8 props (RT scores are `number`, not rich_text).
- [x] One entry that reads the Watchlist and prints entries + property values.
      → `__main__.py`; `--capture-fixture` flag saves the query JSON.

**Verification:** run it; see the real rows with correct property names; confirm
`data_source_id`. Capture the query response as `tests/fixtures/notion_query.json`.
→ **PROVEN live:** `data_source_id = ffcdcd68-…` confirmed; 100 Entries matched; all 8 §8
property names present (✓); `tests/fixtures/notion_query.json` captured (100 results,
`has_more: true`). Blank-Entry row reads as `title=None` (Phase 3 will `failed`/skip it).

---

## Phase 2 — OMDb lane, single source, end-to-end

Goal: `read_page → OMDb → write IMDb rating + plot + genre` for one Entry. Build as a
**1-node `StateGraph`** from the start (avoids a later port). ADR 0004.

- [x] OMDb client: search `?s=` (candidate-shaped from the start, per §4 / ADR 0006) and
      details `?i=<imdbID>`; pass `type=movie|series` from the `Type` select when set.
- [x] **Resolution logic — handle count ∈ {0, 1} only now:** 1 result → proceed; 0 results →
      `failed` (definitive not-found). Leave a clear TODO for the multi-candidate branch
      (phase 6a) — the `?s=` shape is chosen so 6a is *additive*, not a rewrite.
- [x] Notion **write path** (first time): serialize `number` (IMDb rating), `rich_text`
      (plot, genre); idempotent upsert by `page_id`; set `Enrichment Status`.
- [x] `read_page → omdb → update_notion` as a single-node-chain StateGraph.

**Verification:** run one known row → 3 fields + `done` appear in Notion; re-run → identical,
no duplicate writes (idempotency). Capture an OMDb response fixture + one 0-result fixture.

---

## Phase 3 — reconcile() + status lifecycle + single-flight + cron

Goal: the one entrypoint that sweeps the Watchlist. ADR 0001 / 0004.

- [x] **3a — reconcile + lock + status:** query `is_empty OR equals pending` → loop rows
      through the phase-2 graph with `max_concurrency` 3–5 → set `done` / `failed` / `pending`
      per ADR 0004. In-process `asyncio.Lock` single-flight; extra triggers ack/log/drop.
      → `app.py` `Runtime`: owns the clients + the one compiled graph + the single-flight
      `asyncio.Lock`. `reconcile()` checks `lock.locked()` (atomic — no `await` before the
      `async with`) → drops a concurrent trigger; `_sweep()` gathers per-Entry `ainvoke`s under
      an `asyncio.Semaphore(RECONCILE_CONCURRENCY=4)`. Status is written by the graph nodes;
      reconcile tallies a `ReconcileSummary`. **Offline-proven** (stubbed graph): 2nd concurrent
      trigger dropped; peak concurrency ≤ cap.
- [x] **Transient-vs-definitive error classification:** only definitive 0-results → `failed`;
      transient OMDb/network errors leave the Entry `pending` (cron retries). Always write
      partial data before setting status.
      → Definitive outcomes (`failed` on blank/0-result, `pending` on multi-candidate) are
      written by the graph nodes; a transient error raises before the terminal write, so
      nothing is written and the Entry stays pending. `_run_one` catches it → never writes
      `failed`. Single terminal write already satisfies "partial data before status" (becomes
      load-bearing in Phase 4's best-effort RT). **Offline-proven:** a raising graph → counted
      as a transient error, sweep survives, status NOT set to `failed`.
- [x] **Pull the Notion rate limiter forward to here** (`aiolimiter` ≤3 rps, honor
      `Retry-After`) — the ~100-item backfill is exactly the 429-storm ADR 0001 fears.
      → `aiolimiter==1.2.1` pinned. `NotionClient._request()` acquires an `AsyncLimiter`
      (`NOTION_RPS=3`/s) per call and retries 429s honoring `Retry-After` (≤3×); all three
      call sites (query/get/patch) route through it. One client per process = process-global
      throttle (Phase 7 shares it with HITL resume).
- [x] **Manual "run now" = CLI/function** (NOT Slack yet — that's 6c).
      → `--reconcile` (one sweep) and `Runtime.reconcile()`. Slack `@movie-bot run` is 6c.
- [x] **3b — in-process cron:** hourly scheduler calling `reconcile()` (shortened interval for
      testing).
      → `Runtime.run_forever()` plain-asyncio loop (no APScheduler dep; this *is* ADR 0009's
      long-lived process); `--serve` CLI. Period = `RECONCILE_INTERVAL_SECONDS` (default 3600;
      shorten via env). A failed cycle is logged and the loop continues.

**Verification:** run reconcile on the real ~100-item backfill; watch statuses transition;
re-run → only pending/stragglers picked up; trigger twice concurrently → second dropped
(lock proven); shorten cron interval → fires. Fixture: a multi-row query for offline tests.
→ Single-flight drop, concurrency cap, and transient→pending classification **proven offline**
(stubbed graph). Multi-row fixture: `tests/fixtures/notion_query.json` (100 rows, Phase 1).
**Live ~100-item backfill is owner-run** (Notion/OMDb creds aren't in Claude's shell env):
`uv run python -m notion_db_updater --reconcile`, then re-run, then `--serve` with a short
`RECONCILE_INTERVAL_SECONDS`. LangSmith shows one trace per Entry (named by Entry).

---

## Phase 4 — RT resolution subgraph + fan-out/fan-in (Firecrawl only)

Goal: parallel OMDb ‖ RT lanes, RT as a subgraph. **First `with_structured_output`.** ADR
0003 / 0008. (Built before the Judge so the Judge later sees a full record.)

- [x] RT subgraph skeleton with **Firecrawl `/search` only** (scoped to rottentomatoes.com +
      content scrape); fallbacks are phase 8.
      → `firecrawl.py` async `FirecrawlClient` (thin httpx, mirrors `omdb.py`; ports spike-05
      `/search`+`pick_rt_hit`, v2→v1 fallback, `maxAge`=1wk cache). `rt.py` `build_rt_subgraph`
      = compiled `StateGraph` (`firecrawl_provider → extract`), embedded as the `rt` **node** of
      the parent graph (xray-confirmed: subgraph internals nest in LangSmith/Studio). **Year
      caveat:** RT runs *parallel* to OMDb so it can't use OMDb's year and §8 carries none — query
      is title-only; `pick_rt_hit` biases `/m/` vs `/tv/` by `media_type` instead (proven offline).
      **Identity gap (deferred to Phase 5):** `search_rt` returns only the markdown and the lane
      surfaces only `{rt_critic, rt_audience}` — the matched RT page URL/title is *dropped*, so
      the future Judge can't yet detect a cross-lane title mismatch. Phase 5 surfaces it (ADR
      0003 / 0008 updated).
- [x] Per-provider **LLM extraction** node: `with_structured_output` → `{rt_critic,
      rt_audience}` (`OPENAI_EXTRACTION_MODEL`). The extraction result drives chain
      fall-through (success = ≥1 usable score).
      → `rt.extract` — the project's **first `with_structured_output`** (`RTScores` Pydantic,
      `int|None` 0-100). `llm.py` `extraction_model()` factory (the 1st of 3 ADR-0008 roles).
      Applies the spike-05 learning: `_score_region()` anchors on a score marker
      (Tomatometer/Popcornmeter/…) then takes a 12k window capped at 20k — *not* a blind
      `[:8000]` (which dropped scores sitting ~15k chars in). No markdown → null without an LLM call.
- [x] **Fan-out** OMDb ‖ RT subgraph; **deterministic fan-in `assemble`** node (barrier: both
      lanes complete first). Write `rt_critic` / `rt_audience` (best-effort; null-safe).
      → `graph.py` rewired: `read_page →{omdb, rt}`, `{omdb, rt}→ assemble → update_notion`
      (topology verified). Lanes write **disjoint** channels (omdb: status+imdb/plot/genre; rt:
      rt_critic/rt_audience) → no reducer needed on the concurrent fan-out. `assemble` is a thin
      pass-through today, reserved as Phase 5's Judge slot. `enrichment_properties` extended with
      null-safe RT `number` props (`RT Critic/Audience Score`).
- [x] Confirm RT absence never blocks `done` (ADR 0004): soft-miss Entry stays `done` with null
      RT.
      → RT lane **swallows its own errors** (soft miss *and* hard Firecrawl failure → null
      scores, never raises) — contrast the OMDb lane where a transient error propagates → Entry
      stays `pending`. Proven offline: blank Entry skips the search; a raising Firecrawl client
      degrades to `{rt_markdown: None}`. **Known Phase-4 cost:** no `RetryPolicy` (Phase 7) /
      fallbacks (Phase 8) yet → a *transient* Firecrawl error = permanently-null RT on a `done`
      row until a manual re-run.

**Verification:** a title with known RT scores → critic + audience written alongside IMDb; a
soft-miss title → RT null but still `done`; LangSmith trace shows OMDb and RT ran
concurrently and fan-in waited. Fixtures: Firecrawl markdown (hit + soft-miss).
→ **Offline-proven** (deterministic logic): fan-out/fan-in topology, subgraph nesting (xray),
`pick_rt_hit` canonical+media_type bias, `_score_region` slicing, blank-skip, error-swallow.
Fixtures captured: `tests/fixtures/firecrawl_rt_hit.md` (real Dune RT markdown, scores ~15k in)
+ `firecrawl_search_soft_miss.json` (no RT page → `pick_rt_hit`=None). **Live happy-path +
soft-miss are owner-run** (Firecrawl/OMDb/LLM creds aren't in Claude's shell env):
`uv run python -m notion_db_updater --reconcile` on a known-RT row, then a no-RT-page row;
LangSmith should show OMDb ‖ RT concurrent under one trace and `assemble` waiting for both.

---

## Phase 5 — EnrichedEntry schema + LLM-as-judge + RT correlation

Goal: formalize the output contract, add the wrong-match guard, and make the RT lane
candidate-shaped so the Judge can correlate RT pages against OMDb's resolved identity.
**Confidence trace-only.** ADR 0003 / 0008.

Target topology (two new nodes; `assemble` graduates from no-op to real work):

    read_page ──→ {omdb, rt} ──→ assemble ──→ resolve_rt ──→ judge ──→ update_notion ──→ END

**Decisions of record (this phase):**
- **Assemble stays deterministic; the Judge is a *new* node after it** (ADR 0008: deterministic
  assembly feeds the Judge, which supplies `confidence`). `assemble` builds the `EnrichedEntry`
  + `sources_used`; `judge` sets `confidence`. No merge/reconcile — the lanes wrote disjoint
  channels.
- **No `add_conditional_edges` yet** — the new nodes self-guard on `status` / candidate-count.
  Conditional *routing* is Phase 6a's learning slice; adding it here would churn topology twice.
- **Judge (and correlation) are best-effort** — they never block `done`; on LLM failure/skip
  `confidence` degrades to `low` (surfaces the row for review). `confidence` is **trace-only**,
  never a §8 property (§8 unchanged).
- **RT correlation is IN** (ADR 0003 / 0008 extension, "hybrid escalation"): keep the
  deterministic single-pick fast path for 1 canonical RT page; surface a candidate set and
  correlate only when **>1** canonical page is in contention (the *Orphan Black* tail).
- **Correlation lives at a post-fan-in node, not in the RT subgraph** — the lanes run *in
  parallel*, so the RT lane can't see OMDb's identity at scrape time. `resolve_rt` runs after
  the barrier, where both identities exist.

### 5a — `EnrichedEntry` contract + cross-lane identity plumbing (prerequisite)

Both lanes currently *discard* the identity the Judge needs (`search_rt` drops the matched
page URL; `details_fields` drops the year). Fix that first — nothing downstream works without it.

- [x] Pydantic `EnrichedEntry` (RESEARCH §5) in a new `schema.py`, the graph's output contract:
      `title, year:int|None, media_type:Literal["movie","tv"], imdb_id, imdb_rating, rt_critic,
      rt_audience, plot, genre, confidence:Literal["high","medium","low"], sources_used:list[str]`.
      → `schema.py` created; only `title`/`media_type`/`confidence` required, the rest nullable
      (enrichment is best-effort). Aliased `MediaType`/`Confidence` `Literal`s reused across nodes.
- [x] Helpers: `parse_year` (`"2013–"` → `2013`, first 4 digits) and `normalize_media_type`
      (OMDb `movie/series/episode` + Notion `Movie/TV Show` → `movie/tv`; fallback `movie`).
      → `parse_year` lives in `omdb.py` (an OMDb-field parser, beside `parse_rating`);
      `normalize_media_type` in `schema.py`. Both offline-proven.
- [x] **OMDb identity:** extend `details_fields()` to also return `year` (via `parse_year`) +
      `omdb_title` (resolved canonical `Title`); add `year`, `omdb_title` to `EnrichmentState`.
      → Also added `omdb_type` (drives `EnrichedEntry.media_type`). All three in `EnrichmentState`.
- [x] **RT identity:** `firecrawl.search_rt()` returns the matched hit (`RTHit{url, title,
      markdown}`), not bare markdown; `firecrawl_provider` surfaces `rt_url`, `rt_title`,
      `rt_year` (parse trailing `_YYYY` from the slug/title). Add these to `RTState` **and**
      `EnrichmentState` (shared keys, disjoint from OMDb's → still no reducer). Update the
      firecrawl.py `search_rt` docstring example + retire the Phase 4 "identity gap" note above.
      → `search_rt` replaced by `search_rt_candidates() -> list[RTHit]` (candidate-shaped for 5c,
      supersedes the old single-markdown return); `RTHit{url,title,year,markdown}` carries the
      inline-scraped markdown too. `_slug_year` parses the RT slug year. Docstring updated.

### 5b — deterministic assemble + LLM-as-judge

- [x] `assemble` (was a no-op): build the `EnrichedEntry` from state (minus `confidence`) and
      compute `sources_used` (`["omdb"]` + `["firecrawl"]` when the RT lane ran). Deterministic
      fan-in barrier — no LLM. (A reducer-tracked `sources_used` channel is a possible learning
      enhancement; the deterministic compute avoids a parallel-write reducer for now.)
      → **Refinement:** `assemble` computes `sources_used` only; the `EnrichedEntry` is built once
      in `judge` (the sole point where every field is final — RT scores may be rewritten by
      `resolve_rt`, and `confidence` is the Judge's own output, a required field). `assemble`
      self-guards on `status=="done"`.
- [x] `judge_model(settings)` factory (`OPENAI_JUDGE_MODEL`; sibling of `extraction_model`, the
      3rd ADR-0008 role — env var already in `config.py`).
      → In `llm.py`; also drives `resolve_rt` (same identity-judgment role family).
- [x] `judge` node (`with_structured_output` → `JudgeVerdict{confidence, wrong_match:bool,
      reason:str}`): guard on `status=="done"` (skip `failed`/`pending`). Prompt with **both
      identities** — OMDb title/year/imdb_id/rating/plot/genre ‖ winning RT title/url/year/
      critic/audience — to flag **(a) cross-lane title/year mismatch** (RT page ≠ OMDb title)
      and **(b) score implausibility** (IMDb 9.1 vs RT 18%, plot/title mismatch). Best-effort:
      swallow LLM errors → `confidence="low"`. Finalize `confidence` on the assembled record.
      → Guard + best-effort degrade offline-proven (skips `failed`/`pending` without an LLM call).
- [x] `build_graph` gains a `judge_llm` param; add the `judge` node + rewire; update call sites
      (`app.py`, `__main__.py` ×2). `update_notion` unchanged — `confidence` is **not** written.
      → Topology `assemble → resolve_rt → judge → update_notion` verified via `get_graph()`.
      `__main__ --enrich` now prints `confidence`/`wrong_match`/RT page for local verification.

### 5c — RT candidate-set correlation (ADR 0003 / 0008 extension)

- [x] `rank_rt_hits()` returns the ranked canonical RT hits (`pick_rt_hit` becomes
      `rank_rt_hits()[0]`); `firecrawl_provider` surfaces `rt_candidates:
      list[RTCandidate{rt_url, rt_title, rt_year, rt_markdown}]` (markdown already scraped inline
      — free) into `RTState` + `EnrichmentState`.
      → Candidate type is the `RTHit` from 5a (carries markdown), so one type serves both. Only
      *canonical* `/m/`,`/tv/` title pages are candidates (deep links can't score → dropped).
- [x] **Hybrid escalation** in the RT subgraph's `extract`: 0 canonical → null; exactly 1 →
      extract inline (today's fast path); **>1 → defer** extraction, carry the candidate set
      (metadata + already-paid markdown) up to the fan-in for correlation.
      → Offline-proven for 0 / >1 (no LLM call); the exactly-1 fast path extracts inline.
- [x] `resolve_rt` node (parent graph, between `assemble` and `judge`): guard on `status=="done"`
      **and** `len(rt_candidates)>1`. **Metadata-first** — LLM correlates candidate title/year
      against OMDb's resolved title/year (cheap), picks the winner (or none → soft miss), then
      **extracts scores once** from the winner's markdown (shared `extract_rt_scores` helper
      refactored out of `rt.extract`). Sets `rt_url/title/year/critic/audience`.
      → Uses `judge_model`. `RTMatch{index:int|None, reason}` structured output. **On an LLM
      error it falls back to the top-ranked candidate** (RT stays best-effort); an out-of-range
      index is clamped to 0. **Plot-aware correlation:** RT extraction now also pulls the
      synopsis (`RTPage{plot, rt_critic, rt_audience}`; single `extract_rt_page`), so the
      correlation prompt weighs OMDb's `plot` against each candidate's synopsis slice
      (`synopsis_region`, cheap — no per-candidate LLM), not just title/year. `rt_plot` is added
      to state (winner's, extracted once) and also fed to the Judge for plot-mismatch detection.
- [x] **Ordering coupling** (ADR 0008): correlation needs OMDb's *resolved* identity, so a
      still-ambiguous OMDb (multi-candidate → `pending`, Phase 6a) never reaches `resolve_rt`
      (the `status=="done"` guard skips it) and is retried after 6a resolves it — self-healing,
      no hard dependency.
      → Enforced by the `status=="done"` guard in `resolve_rt` (and `judge`).
- [x] **Checkpoint hygiene:** candidate markdown in state will bloat the Phase 6 checkpoint —
      drop `rt_markdown` / `rt_candidates` from state once `resolve_rt` picks a winner.
      → `resolve_rt` returns `rt_candidates: []` on both the winner and no-match paths.
- [x] **Confidence stays in graph state / LangSmith only** — NOT written to Notion. Document how
      to find low-confidence rows (filter LangSmith by the `confidence` / `wrong_match` metadata).
      → `update_notion` (`enrichment_properties`) writes no confidence — §8 untouched. Low-
      confidence rows: filter the LangSmith project by the `judge` node's `confidence` output.

**Verification:** force a bad `imdbID` → Judge returns low confidence / wrong-match flag; point
the RT lane at a *different* title than OMDb resolved → Judge flags the cross-lane mismatch (the
case scores-only could not catch); a clean match → high; an *Orphan Black*-style query (>1
canonical RT page) → `resolve_rt` picks the page matching OMDb's year and extracts scores once.
Confirm `confidence` appears in the LangSmith trace, not in Notion. Fixtures:
`firecrawl_rt_multi_candidate.json` (>1 canonical RT page) + a deliberately-wrong assembled
record (mismatched OMDb title vs RT page). Offline-provable: `EnrichedEntry` build, year /
media_type normalization, identity plumbing, `sources_used`, `rank_rt_hits`, node skip-guards.
Live judge + correlation are owner-run (LLM creds aren't in Claude's shell env).
→ **Offline-proven** (`ruff` clean; `uv run` checks): `parse_year` / `normalize_media_type` /
`_slug_year` / `rank_rt_hits`; graph compiles with the new topology (`assemble → resolve_rt →
judge → update_notion`); `assemble`/`resolve_rt`/`judge` skip-guards + `rt.extract` hybrid
escalation all short-circuit `failed`/`pending`/0/1-candidate state **without** an LLM call.
Fixtures captured: `tests/fixtures/firecrawl_rt_multi_candidate.json` (>1 canonical RT page →
`rank_rt_hits` yields Dune 2021 + 1984, drops deep-link/off-domain) +
`tests/fixtures/judge_wrong_match.json` (a `status=done` record with an *Orphan Black* cross-lane
title/year mismatch). **Live happy-path + wrong-match + >1-candidate correlation are owner-run**
(LLM/Firecrawl/OMDb creds aren't in Claude's shell env): `uv run python -m notion_db_updater
--enrich <page_id>`; LangSmith should show `resolve_rt` firing only on the >1 tail and
`confidence` in the `judge` node output (absent from Notion).

---

## Phase 6 — HITL disambiguation (split 6a–6d)

Goal: conditional routing + `interrupt()` + durable resume + Slack. ADR 0006 / 0007 / 0010.

- [x] **6a — checkpointer + many-candidate path + pre-filter (auto-pick, no human):**
  - [x] Compile graph with `AsyncSqliteSaver` on a `.sqlite` file, `thread_id = page_id`.
        → `build_graph(..., checkpointer=)` param; `Runtime` opens the saver in `__aenter__`
        via an `AsyncExitStack` (`saver.setup()` is async), compiles the one shared graph there,
        and closes it in `aclose`. `_run_one` passes `configurable.thread_id = page_id`.
        `--enrich` opens its own saver so single-Entry runs checkpoint identically. New config
        `CHECKPOINT_DB_PATH` (`.env.example` updated; `*.sqlite` already gitignored).
  - [x] Switch OMDb resolution to surface the **full candidate list** (the phase-2 TODO).
        → The single `omdb` node is split into **`omdb_search`** (search → candidates; 0 →
        `failed`, 1 → trivial `chosen_imdb_id`, >1 → carry the list) and **`omdb_details`**
        (fetch details for `chosen_imdb_id`; no-op when none). The split is *forced*: `details`
        needs the post-pick id, and it must stay out of any node a 6b `interrupt()` would re-run.
        The OMDb lane always terminates at `omdb_details`, so the fan-in stays a clean two-edge
        barrier (no conditional edge lands on `assemble`).
  - [x] LLM **disambiguation pre-filter** (`OPENAI_DISAMBIGUATION_MODEL`) picks the best
        Candidate; `add_conditional_edges` route that *always* takes the pick for now.
        → `disambiguation_model()` factory (3rd of the 3 ADR-0008 roles). `disambiguate` node:
        `with_structured_output` → `DisambiguationPick{index, confident, reason}` (index →
        validated candidate). `add_conditional_edges("omdb_search", route_after_search, …)` sends
        >1 → `disambiguate`, ≤1 → `omdb_details`; `disambiguate → omdb_details` is a **plain
        edge** (6a always takes the pick — 6b makes it conditional on `confident`). Unlike the
        best-effort RT/Judge nodes, `disambiguate` **fails safe**: an LLM error or an unusable
        index yields `confident=False` + no `chosen_imdb_id` (defer to a human in 6b), never a
        guessed identity. Pick stashed as `best_guess_imdb_id` for the 6d timeout.
  - [x] **Verify:** a multi-candidate Entry (e.g. *Dune*) resolves to the pre-filter's choice;
        checkpoint rows appear in the `.sqlite` file.
        → **Offline-proven** (`ruff` + `basedpyright` clean; stubbed-client graph runs): all
        four routes execute correctly through the conditional edge + fan-in barrier
        (multi→disambiguate→done, single→details→done, not-found→failed, blank→failed), and the
        checkpoint persists under `thread_id=page_id` (`aget_state` → `status=done`, `next=()`).
        Pure-logic checks: `route_after_search`, `omdb_details` no-op, `disambiguate` fail-safe
        (LLM error + out-of-range index) and happy path. **Live multi-candidate (real LLM pick +
        `.sqlite` inspection) is owner-run** (LLM/OMDb creds aren't in Claude's shell env):
        `uv run python -m notion_db_updater --enrich <page_id>` on a *Dune*-like row → prints the
        pre-filter's chosen imdb_id / confident / reason; `sqlite3 checkpoints.sqlite '.tables'`
        shows the checkpoint rows.
        - [x] **Deferred to 6b:** register `Entry`/`Candidate`/`EnrichedEntry` for msgpack serde
              (LangGraph 1.2.6 warns "unregistered type … blocked in a future version").
              → Fixed via **allowlist**, not type conversion: the warning fires for *any* custom
              type (proven — the already-pydantic `EnrichedEntry` warned identically), so
              dataclass-vs-pydantic is irrelevant. New `checkpoint.py` centralizes an
              `open_checkpointer()` that builds the saver with
              `JsonPlusSerializer(allowed_msgpack_modules=[(mod,name)…])` listing `Entry` /
              `Candidate` / `RTHit` / `EnrichedEntry` (module+name tuples — bare module strings
              silently *block*). Wired into all three saver sites (Runtime, `--enrich`,
              `--resume`). Round-trip through disk proven by the restart test below.
- [x] **6b — interrupt() + programmatic resume + restart survival:**
  - [x] When the pre-filter is unsure → `interrupt()`; set `awaiting_input`; reconcile
        "moves on" to the next Entry.
        → New `await_human` node calls `interrupt(_picker_payload)` — a *dedicated* node, not
        `interrupt()` inside `disambiguate` (interrupt re-runs its whole node on resume, so the
        LLM pick must not re-fire; same isolation rationale as the 6a search/details split).
        `route_after_disambiguate` routes confident→`omdb_details`, else→`await_human`.
        `_run_one` detects `__interrupt__` in the returned state, writes `awaiting_input`, and
        returns the new `_AWAITING` tally bucket; the sweep filter (empty/pending) already skips
        `awaiting_input`, so it naturally moves on (ADR 0006).
  - [x] Resume via `graph.invoke(Command(resume=<imdbID>), thread_id=page_id)` from a test
        harness (no Slack yet).
        → `Runtime.resume(page_id, imdb_id)` (no single-flight lock — sweep-vs-resume is
        partitioned by status, ADR 0006), exposed as `--resume PAGE_ID IMDB_ID`. `--enrich`
        prints the picker payload + the resume command when a row pauses.
  - [x] **Verify:** ambiguous row pauses (`awaiting_input`, `invoke` returns); manual resume
        completes it; **kill + restart the process between interrupt and resume → resume still
        works** (durable execution — the core learning target).
        → **Offline-proven** (`ruff` + `basedpyright` clean; stubbed-client graph on a real
        on-disk saver, whole run under warnings-as-errors so any serde warning fails): a >1
        candidate + `confident=False` pauses at `await_human` (`__interrupt__` set, `next =
        ('await_human',)`, best-guess stashed); `Command(resume=…)` completes it to `done` with
        the chosen imdb_id. **Restart survival:** pausing under one saver, closing it, then
        reopening a *fresh* saver + recompiled graph on the same `.sqlite` file reloads the
        paused state (`entry`/`candidates` deserialize back as real `Entry`/`Candidate`) and
        resume still finishes to `done`. Pure-logic: `route_after_disambiguate`, the
        `awaiting_input` tally + Notion payload. **Live** (real LLM unsure-pick + a real process
        kill between pause and resume) is owner-run — LLM/OMDb/Notion creds aren't in Claude's
        shell env: `--enrich <ambiguous page_id>` → prints the pause + candidates, then
        `--resume <page_id> <imdbID>` finishes it; `sqlite3 checkpoints.sqlite '.tables'` shows
        the persisted thread.
- [x] **6c — Slack Bolt Socket Mode transport:**
  - [x] Block Kit picker (≤5 candidates: title + plot + poster; ≤5 buttons; `value` encodes
        `page_id` + `imdbID`) posted to `#notion-movie-db`.
        → New `slack.py`: `build_picker_blocks(page_id, payload)` renders the `interrupt()`
        payload — one `section` per candidate (title · year · type, poster as an image accessory
        when present, best-guess flagged), then one `actions` block of buttons whose `value` is
        `json({page_id, imdb_id})` and `action_id` is `pick:<i>`. **Plot omitted:** OMDb `?s=`
        search carries no plot, so rendering it would cost N extra `?i=` detail calls just to
        draw the prompt; title/year/type/poster disambiguates fine. Needed a new **`aiohttp`**
        dep (slack-bolt's async transport).
  - [x] Action handler → `graph.invoke(Command(resume=imdbID), thread_id=page_id)` (resume does
        NOT take the single-flight lock — coordination is by status, ADR 0006).
        → `SlackTransport` (`AsyncApp` + `AsyncSocketModeHandler`): the `pick:*` action handler
        decodes the button `value` and calls `Runtime.resume(page_id, imdb_id)`, then
        `chat_update`s the message to show who resolved it. Double-click is a safe no-op —
        `Runtime.resume` checks `aget_state().next` and returns the stored status without
        re-invoking when the thread is already finished.
  - [x] Wire `@movie-bot run` → `reconcile()` (single-flight).
        → `app_mention` handler: `run` → `Runtime.reconcile()` (same single-flight lock), replies
        with the summary. The sweep posts pickers via `Runtime.set_notifier(post_picker)`, wired
        in `_serve`, which now runs the cron loop + Socket Mode listener concurrently (Slack
        started only when both tokens are set; else cron-only).
  - [x] **Verify:** a real Slack click resolves a real `awaiting_input` row; double-click is a
        safe no-op (finished-thread resume).
        → **Offline-proven** (`ruff` + `basedpyright` clean): `build_picker_blocks` shape +
        button values decode to `{page_id, imdb_id}` + 5-candidate cap; `SlackTransport`
        constructs with fake tokens, registers both listeners, and `post_picker` posts the
        picker blocks to the channel (client mocked); the double-click guard (`state.next == ()`
        after resolve → no-op) on the real stubbed graph. **Live** (a real Slack click on a real
        `awaiting_input` row + a real double-click) is owner-run — needs `SLACK_BOT_TOKEN` /
        `SLACK_APP_TOKEN` and the app configured for Socket Mode + Interactivity: `--serve` with
        tokens set → an ambiguous row posts a picker in `#notion-movie-db`; clicking a button
        resolves the row to `done`; `@movie-bot run` triggers a sweep.
- [x] **6d — 7-day stale-interrupt auto-resolve:** cron finds `awaiting_input` older than 7
      days → resume with the stored pre-filter best-guess + `confidence: low`.
      → New `Runtime.auto_resolve_stale(max_age=STALE_INTERRUPT_TIMEOUT_SECONDS)`: a *separate*
      pass (own `NotionClient.query_awaiting_input()` filter — deliberately NOT merged into the
      reconcile sweep filter, which would re-run paused rows and break status-partitioning).
      Age comes from the **checkpoint's own `created_at`** (the paused thread's latest checkpoint
      *is* the `interrupt()` snapshot → exact pause moment, and free since we read the checkpoint
      anyway for the best guess) — not Notion's `last_edited_time`. Resumes via the existing
      `Runtime.resume(page_id, best_guess, auto_resolved=True)`; the flag rides in on
      `Command(resume=, update={"auto_resolved": True})` (a standalone `aupdate_state` on the
      fan-in-interrupted graph is rejected as an "ambiguous update"), so `judge` short-circuits to
      `confidence="low"` (trace-only, flagged for review) **without an LLM call**. Wired into the
      cron loop (`run_forever`) after `reconcile()`, same try/except. New `--auto-resolve-stale`
      (+ `--stale-timeout SECONDS`) CLI. **Decision:** a fail-safe escalation (LLM error / no
      valid index) stashed no best guess → left `awaiting_input` + logged (a human can still
      resume it), never guessed-at or `failed` (ADR 0006 "none is lost").
  - [x] **Verify:** with a shortened timeout, an unclicked row auto-resolves to `done`/low.
        → **Offline-proven** (`make check` clean; stubbed clients on a real on-disk saver): a
        paused row (`confident=False`, best guess stashed) with `max_age=0` resumes to `done`,
        `chosen_imdb_id == best_guess`, `confidence == "low"`, `auto_resolved` set, and **no LLM
        judge call fires** (the stub raises if `JudgeVerdict` is requested); with a huge timeout
        the same rows are left `fresh`; a no-best-guess row is left `awaiting_input` (tally
        `no_guess`); a re-run is idempotent (resolved rows drop out of the awaiting_input query).
        **Live** (real process, real 7-day-shortened timeout) is owner-run — creds aren't in
        Claude's shell env: `--serve` with a small `STALE_INTERRUPT_TIMEOUT_SECONDS`, or
        `--auto-resolve-stale --stale-timeout 0`; an unclicked `awaiting_input` row auto-resolves
        to `done` on the next cron cycle, trace shows `origin=auto_resolve` + `confidence=low`.
- [x] **6e — manual IMDb input in the HITL picker (reject all candidates):** today the human
      must pick one of the ≤5 shown OMDb candidates; if the right title isn't among them (or none
      is correct) there is no in-Slack escape — it has to be fixed out-of-band by imdbID
      (`--resume PAGE_ID IMDB_ID`, as done manually for *Michael* → `tt11378946`, which OMDb `?s=`
      search never surfaced in the top 5). **Design of record: inline `input` block** (Slack
      supports `plain_text_input` in messages) — NOT a "None of the above" button + follow-up
      message/modal, and **no graph change**: the graph stays paused at the original
      `interrupt()` until a valid imdbID arrives from *either* a candidate button or the input
      field, so restart durability mid-rejection is free and the `await_human → omdb_details`
      edge is untouched. 6e is a Slack-layer-only slice.
      → All changes in `slack.py`. The graph, `Runtime.resume`, and the `await_human` node are
      untouched — 6e reuses the existing resume path verbatim (the same one `--resume` proved for
      the non-candidate *Michael* id).
  - [x] Extend `build_picker_blocks` with an `input` block under the candidate buttons:
        `plain_text_input`, `dispatch_action: true` (`trigger_actions_on: ["on_enter_pressed"]`),
        `action_id` e.g. `manual:submit` — deliberately outside the `pick:\d+` regex so the
        candidate handler can never receive it. Label: "None of the above? Paste the IMDb link".
        → Input block appended after the `pick_actions` block; `action_id="manual:submit"` (not
        matched by `_PICK_ACTION`). The `page_id` rides in the block's `block_id`
        (`manual_input:<page_id>`) since input elements carry no per-action `value` — keeps the
        handler self-contained without re-parsing the candidate buttons.
  - [x] Slack handler on `manual:submit`: extract the imdbID (`tt\d+`) from the pasted text
        (full URL or bare id); on garbage, `ack()` + reply with a correction hint (message
        inputs have no modal-style inline validation) and leave the picker up; on a valid id,
        resume via the same `Command(resume=<imdbID>)` path — the mechanism `--resume` already
        proves works for a non-candidate id.
        → New `handle_manual` action handler: `_IMDB_ID = re.compile(r"tt\d+")` extracts the id
        from a full URL or a bare id; garbage → `chat_postEphemeral` hint (no `chat_update`, so
        the picker stays clickable); valid → `Runtime.resume(page_id, imdb_id)` then the shared
        `_resolved_blocks` terminal message (refactored out of `handle_pick`).
  - [x] **Caveat to respect:** `chat_update` on the picker wipes any half-typed input — keep the
        existing behavior of only updating the message on terminal resolution, never mid-wait.
        → The garbage path uses `chat_postEphemeral`, never `chat_update`; the only `chat_update`s
        are post-submit (resolving… → resolved), i.e. terminal.
  - [x] **Verify:** an ambiguous row whose correct match is *not* in the top-5 → paste an IMDb
        link into the picker's input → resolves to `done` with the human's identity (repro:
        *Michael*); pasting garbage → error reply, picker still clickable.
        → **Offline-proven** (`make check` clean; throwaway script driving the handler with a
        mocked Slack client): the input block carries `dispatch_action`+`on_enter_pressed` and an
        `action_id` outside `pick:\d+`; a full imdb.com URL *and* a bare `tt…` id both resume with
        the extracted id; garbage → `chat_postEphemeral` fires, `resume` is NOT called and the
        picker is NOT `chat_update`d. **Live** (a real Slack paste on a real `awaiting_input` row,
        repro *Michael*) is owner-run — Slack tokens aren't in Claude's shell env: `--serve` with
        tokens set → paste an IMDb link into an ambiguous row's picker → resolves to `done`;
        pasting garbage → ephemeral hint, buttons still clickable.
- [x] **6f — unmatchable / malformed titles → escalate, don't silently `failed`:** when
      `omdb_search` returns 0 candidates, the row is written terminal `failed` and dropped from
      the sweep — but most such failures are *title-matching* misses, not "doesn't exist".
      Normalize the title before search, and on a still-empty result escalate to the 6e human
      path instead of `failed` (reserve `failed` for genuine not-founds). Observed from the
      backfill (all resolved by hand to real imdbIDs):
  - Season/qualifier suffixes OMDb can't search: `Beef Season 2` → *Beef* (`tt14403178`),
    `Fallout (Season 2)` → *Fallout* (`tt12637874`), `The Bear (S04)` → *The Bear*
    (`tt14452776`), `The Punisher - One Last Kill` → *The Punisher: One Last Kill* (`tt36042156`)
  - Misspellings: `The Oddessey` → *The Odyssey* (`tt33764258`)
  - Punctuation / spelling variants: `Your Friends and Neighbours` → *Your Friends & Neighbors*
    (`tt30459041`), `The Man from U.N.C.L.E` → *The Man from U.N.C.L.E.* (`tt1638355`)
  - Regional / alternate titles: `Department Q` → *Dept. Q* (`tt27995114`), `Ne Zha II` →
    *Ne Zha 2* (`tt34956443`)
  - [x] Normalize before search: strip trailing `Season N` / `(SNN)` / `(Season N)` qualifiers
        (series enrich at the series level anyway); normalize `and`↔`&` and stray punctuation.
        → `omdb.normalize_title(title) -> list[str]` (pure): ordered *mechanical* fallback
        variants (season suffix stripped, `and`↔`&`, punctuation → spaces), excluding the
        original + dupes. `omdb_search` searches the title as written, then each variant until one
        returns candidates (logs which). Deliberately not a spell/abbrev fixer — misspellings
        (*The Oddessey*), abbreviations (*Dept. Q*), roman numerals (*Ne Zha II*) fall through to
        the human path.
  - [x] On a still-empty search, route to the 6e human path (post the picker with no candidate
        buttons — just the manual IMDb-link input) instead of writing `failed`.
        → `omdb_search` no longer sets `failed` on a still-empty result — it carries an empty
        candidate list; `route_after_search` sends `0` candidates → `await_human` (blank Entry
        still → `omdb_details` passthrough, keeps `failed`). `_picker_payload` sets
        `reason="not_found"` for the candidate-less case; `build_picker_blocks` drops the (invalid)
        empty `actions` block and shows a "Couldn't find …" header, leaving 6e's manual input as
        the sole control. **Genuine not-found → `failed` via the 6d timeout** (decision of
        record): `await_human` resolves a `NOT_FOUND` sentinel resume to terminal `failed`, and
        `auto_resolve_stale` now splits a no-best-guess stale row by candidate presence —
        0 candidates (6f not-found) → resume `NOT_FOUND` → `failed` (new `not_found` tally);
        >0 candidates (disambiguate LLM fail-safe) → left `awaiting_input` (`no_guess`, 6d
        original). Slack-layer only for the picker; graph + 6d touched for routing/terminal.
  - [x] **Verify:** each example above ends `done` (via normalization or a human paste); a
        genuinely nonexistent title still ends `failed`.
        → **Offline-proven** (`make check` clean; two throwaway scripts on a real on-disk saver,
        warnings-as-errors): `normalize_title` yields the expected variant for every backfill
        example; `Beef Season 2` matches via the `Beef` fallback → `done`; a searched-empty title
        pauses at `await_human` (`reason=not_found`, candidate-less payload, *not* `failed`); a
        `NOT_FOUND` resume → `failed` written to Notion; a non-candidate imdbID paste → `done`
        (repro *Michael*); a blank Entry still → `failed` without searching or pausing; the
        candidate-less picker renders no `actions` block, a "Couldn't find" header, and keeps the
        manual input. Stale pass (`max_age=0`): a 0-candidate row → `failed` (`not_found`), a
        2-candidate fail-safe row → left `awaiting_input` (`no_guess`), untouched in Notion.
        **Live** (real OMDb/LLM/Notion; the backfill examples) is owner-run — creds aren't in
        Claude's shell env: `--reconcile` → season/`&`/punctuation rows resolve via normalization;
        a misspelling/regional title escalates to a candidate-less picker (paste the imdbID →
        `done`); gibberish escalates then, with a shortened `STALE_INTERRUPT_TIMEOUT_SECONDS`,
        auto-resolves to `failed`.

---

## Phase 7 — Resilience

Goal: make the sweep robust. ADR 0001 / 0004 / 0007, **new ADR 0013** (retry in two layers +
three-layer backpressure). (LangSmith already on since phase 1.)

- [x] Per-node `RetryPolicy` (exp backoff + jitter; retries transient 429/5xx, NOT `ValueError`)
      — one `is_transient` predicate in new `resilience.py`, applied via `build_graph` to the
      **gating nodes only** (`read_page` / `omdb_search` / `omdb_details` / `update_notion`).
      Best-effort nodes (RT lane, LLM) deliberately retry *client-side* instead, since a node
      RetryPolicy's exhaustion re-raises and would break "RT never blocks `done`" (ADR 0013).
- [x] Remaining per-API limiters (`aiolimiter`): Firecrawl `FIRECRAWL_RPM` (~10/min), process-
      global (per-client instance), shared by sweep + HITL resume, alongside its internal
      transient retry (honors `Retry-After`). (Notion limiter landed in phase 3; **Tavily + Exa
      limiters land with those clients in Phase 8** — they don't exist yet.)
- [x] Shared `InMemoryRateLimiter` (`LLM_RPS`, 0 = off) across all three role models (aggregate
      cap on the one endpoint) + `LLM_MAX_RETRIES`; `max_concurrency` capped per `ainvoke` via
      `GRAPH_MAX_CONCURRENCY` on top of the sweep semaphore.
- [x] Batch isolation: `_sweep` gathers with `return_exceptions=True` (one bad Entry can't cancel
      its siblings); per-Entry checkpoints (`thread_id = page_id`) already isolate state.

**Verification:** offline — `is_transient` true for 429/5xx/transport, false for `ValueError` /
404 / `ValidationError`; `FirecrawlClient` retries a stubbed 503 then succeeds, raises a 404
without retry, and the RT lane swallows an exhausted 503 to null RT (best-effort intact); gating
nodes carry a RetryPolicy, others don't. Live (owner-run) — induce a 429/5xx on OMDb → retry +
backoff then `pending`, not a crash; LangSmith shows per-node retries; limiter caps RPM.

---

## Phase 8 — RT provider rotation (Firecrawl ‖ Tavily ‖ Exa, round-robin start)

Goal: distribute RT search load across the three free API tiers. ADR 0003 **amended twice**:
a rotated-start chain supersedes the original fixed Firecrawl-first order, **Perplexity is
dropped** (three rotating providers already give redundancy; one less key), and the chain
lives in an injected **`SearchClient` strategy** behind the subgraph's single `rt_search`
node — not sibling provider nodes + conditional edges. Soft miss is judged at the *search*
layer (zero canonical candidates); a found-but-scoreless page ends the lane with null RT
(accepted: all providers search the same site, a fallback would re-find the same page).
Fan-out was rejected (burns ~3× quota per Entry — against the free-tier goal), as was
single-pick-no-fallback (one soft miss would leave RT null until a manual re-run).

- [x] **Seam (pulled forward, pre-Phase-8):** new `search.py` with the `SearchClient`
      Protocol (`search_rt_candidates(title, media_type) -> list[RTHit]`, search-only — no
      lifecycle), `RTHit`, and the provider-agnostic ranking/slug helpers moved out of
      `firecrawl.py`. Rename the subgraph node + function `firecrawl_provider` → `rt_search`
      (parallel to `omdb_search`); `build_rt_subgraph` / `build_graph` take
      `search: SearchClient` instead of `firecrawl: FirecrawlClient`. `Runtime` still
      constructs/closes the concrete client. Pure refactor — zero behavior change.
      → `search.py` created (`RTHit`, `runtime_checkable SearchClient`, `rank_rt_hits` /
      `pick_rt_hit` / `_slug_year` / `hits_to_rt`). Node + function `firecrawl_provider` →
      `rt_search`; `build_rt_subgraph` / `build_graph` take `search: SearchClient`. `FirecrawlClient`
      satisfies the Protocol structurally (proven via `isinstance`). Committed on its own (Commit 1).
- [x] Add `TavilyClient` + `ExaClient` as **full `SearchClient` peers** of Firecrawl
      (search + inline content + the same candidate shaping — markdown-bearing `RTHit`s;
      parity is what makes rotation safe). Update the config default to
      `firecrawl,tavily,exa` and drop `PERPLEXITY_API_KEY` from `config.py` / `.env.example`.
      → `tavily.py` (`/search`, `include_domains` + `include_raw_content`) + `exa.py` (`/search`,
      `includeDomains` + `contents.text`), both thin httpx clients mirroring `FirecrawlClient`
      (process-global `aiolimiter`, `aclose`). Retry centralized: `search.post_json` + `_retry_delay`
      are the shared transient-retry loop all three providers use (Firecrawl refactored onto it).
      Config default now `firecrawl,tavily,exa`; `PERPLEXITY_API_KEY` dropped; `TAVILY_RPM`/`EXA_RPM`
      added. **Content-format caveat surfaced:** Tavily `raw_content` / Exa `text` are plaintext,
      not Firecrawl markdown — the `rt.py` score/synopsis markers survive in text, but that parity
      is the owner-run live check.
- [x] **`RoundRobinSearchClient` composite** (itself a `SearchClient`), built from
      `SEARCH_PROVIDERS`: a process-local counter (unpersisted — approximate fairness across
      restarts is fine) rotates which provider goes *first* per Entry; the others remain
      fallbacks in rotation order. Advance on hard fail OR zero candidates; retry transient
      blips within a provider first; **first candidate wins** (short-circuit the rest). Log
      the winning provider per Entry (the graph shows one `rt_search` span, so the log line
      is the observability).
      → `search.RoundRobinSearchClient`: `_next` cursor rotates the lead; iterates in rotation
      order, advancing on a raised provider (hard fail — transient retries already exhausted in
      `post_json`) OR an empty candidate list (soft miss); first non-empty wins + logs the winner;
      all-empty → `[]` (best-effort null RT). Lifecycle stays on the concrete children.
- [x] `Runtime` wires the composite when >1 provider is configured, the bare client when 1.
      → New `providers.build_search_client(settings)` async-context-manager factory (maps provider
      names → concrete clients, kept out of `search.py` to avoid the import cycle): bare client for
      1, `RoundRobinSearchClient` for >1, unknown names skipped (fall back to bare Firecrawl),
      closes every client on exit. Wired into `Runtime.__aenter__` (via the `AsyncExitStack`) and
      the `--enrich`/`--generate-graph` CLI paths.

**Verification:** across a 3-Entry sweep, each provider leads exactly once (winning-provider
log lines); force the leading provider to hard-fail and to return zero candidates → the next
in rotation runs; confirm first-candidate-wins short-circuits the rest. Fixtures per provider.
→ **Offline-proven** (`make check` clean; scratchpad verify script, 20/20): rotation leads
a→b→c across 3 entries then wraps; advance on hard-fail *and* zero-candidate with 'c' winning;
first-candidate short-circuits (fallbacks untouched); all-empty → `[]`; composite + fakes
satisfy `SearchClient`; `TavilyClient`/`ExaClient` map their response shapes → 1 canonical
`RTHit` (drop `/reviews` deep link) with score markers preserved in the content; `build_search_client`
yields composite (3) / bare (1) / firecrawl-fallback (unknown) and the graph compiles with each.
Fixtures captured: `tests/fixtures/tavily_rt_search.json` + `exa_rt_search.json` (documented API
shapes, illustrative content). **Live is owner-run** (Firecrawl/Tavily/Exa keys aren't in Claude's
shell env): a 3-Entry `--reconcile` → each provider's "won for …" log line once; force the lead
to hard-fail / return zero → the next in rotation runs; confirm each provider's plaintext content
still carries the RT score markers `rt.py` anchors on. **Also drop `perplexity` from your local
`.env` `SEARCH_PROVIDERS`** (skipped gracefully with a warning otherwise).

---

## Phase 9 — Slack `/add` second entry point (create-then-enrich, out-of-band)

Goal: a `/add <name>` slash command that originates an Entry from Slack, runs the same
search/disambiguation/enrichment, and reports back. **Additional** entry point — the
Notion-origin path is unchanged. ADR 0012 (reuses 0006 / 0010 / 0001 / 0004).

Target topology (one new terminal node; the sweep path is untouched):

    … → update_notion → notify → END

`notify` no-ops for `origin == "sweep"` and posts a completion `chat_postMessage` for
`origin == "slack"`. It sits *after* `update_notion` (Notion is source of truth; Slack is a
ping) and fires on every terminal path — the initial invoke, a Slack-click resume, and the
6d auto-resolve alike — because all three flow through `update_notion → notify`.

**Decisions of record (this phase):**
- **Completion pings go to the `/add` channel; the disambiguation picker stays fixed to
  `#notion-movie-db`.** The picker is reused *verbatim* (no new picker code → fixed
  `SLACK_CHANNEL`, ADR 0012); completions reply where the user typed `/add`
  (`command.channel_id`). Accepted caveat: an `/add` typed in a DM gets its picker in
  `#notion-movie-db` (realistically `/add` is used in-channel).
- **`origin` + notify context are set at invoke time in the initial state dict**, so they are
  checkpointed and survive interrupt/resume/restart (durable, ADR 0012). Resume / auto-resolve
  don't re-supply them — they're already in the checkpoint. The sweep passes neither → `origin`
  defaults to `sweep` and `notify` no-ops.
- **The `notify` node is late-bound** via a mutable `CompletionNotifier` holder injected at
  compile time (the graph compiles in `Runtime.__aenter__`, *before* `SlackTransport` exists —
  the same mutual-reference constraint the picker notifier already has). Unbound (CLI / no
  Slack) → no-op. Best-effort: it swallows Slack errors so a ping failure never fails an
  already-written Entry.
- **In-flight guard is a process-global `set[str]` on `Runtime`**, shared with the sweep, which
  skips any `page_id` in it. Crash-safe by construction: the set vanishes on crash but the row
  is still `pending`, so the next cron reclaims it (ADR 0004 self-heal — no new Notion status).
- **`/add` gibberish escalates to the candidate-less picker first** (Phase 6f: `omdb_search` no
  longer writes `failed` on 0 results). The not-found ping fires only when the row ultimately
  resolves `failed` (a `NOT_FOUND` resume or the 7-day timeout) — TASKS's "gibberish → failed"
  is the *eventual* state, consistent with the notify node firing on the auto-resolve.
- **Dedupe is a title-`contains` query + client-side case-insensitive exact match** — catches
  exact re-adds, not variant spellings (*Dune* vs *Dune: Part Two*), per ADR 0012. No status
  filter (a `done` row must still count as a dupe).

### 9a — graph plumbing: `origin` + notify context + `notify` node + Type backfill (no Slack)

- [x] `EnrichmentState` gains `origin` / `notify_channel` / `notify_user` (all `NotRequired`;
      absent → `sweep`). New `CompletionNotifier` mutable holder (`.bind(post)` / `.notify(…)`),
      unbound → no-op. New `notify` node bound to it: guard on `origin == "slack"`, build a
      done/failed message from `status` + `enriched`, post best-effort (swallow Slack errors).
      Rewire `update_notion → notify → END`; `build_graph` gains a `completion_notifier` param.
      → All in `graph.py`. `notify` sits after `update_notion` (Notion is source of truth); the
      done ping is IMDb/RT/genre + IMDb link, the failed ping a not-found notice. `origin` +
      notify context ride in on the **initial state dict** (checkpointed → durable across
      interrupt/resume/restart, ADR 0012), so `notify` fires on the initial run, a resume, and
      the 6d auto-resolve alike.
      - [x] **Fan-in barrier fix (surfaced by 9a):** the pre-existing fan-in at `assemble`
            actually **double-fired** the whole post-fan-in tail — the OMDb lane is structurally
            longer than the single-node `rt` lane, so its two edges reach `assemble` in different
            supersteps and (without a barrier) LangGraph re-runs the join per arriving edge.
            Latent before Phase 9 (idempotent `update_notion`; trace-only `judge` — though it
            silently ran the judge LLM **twice**), but `notify` would double-post the Slack ping.
            Fixed with `add_node("assemble", …, defer=True)` → a true barrier; the tail now runs
            exactly once (also halves the judge LLM call across all phases). Proven offline on
            both the happy path and the paused→resume path (every node once).
- [x] **Type backfill:** extend `enrichment_properties` with an optional `notion_type` param
      (writes `PROP_TYPE` select) + a `notion_type_value(media_type)` reverse-map
      (`movie`→"Movie", `tv`→"TV Show"). `update_notion` writes `Type` **only** for a resolved
      (`enriched`) `slack`-origin row; sweep rows write no `Type` (§8 behavior unchanged).
      → `models.py`. Backfill keyed off `enriched.media_type` (final at `update_notion`, post-
      judge); a Notion-origin sweep row's human-filled `Type` is never overwritten.
- [x] **Verify (offline):** sweep-origin run → `notify` no-ops, no `Type` written; slack-origin
      run with an *unbound* notifier → no-op, no crash; `notion_type_value` round-trips; graph
      compiles with `update_notion → notify → END`.
      → **Offline-proven** (`make check` clean; scratchpad script on a real on-disk saver,
      warnings-as-errors): sweep row → `done`, 0 pings, no `Type` prop; slack row → `done` +
      exactly 1 ping to the `/add` channel (mentions user + title) + `Type=Movie` backfilled;
      unbound notifier → still `done`, no crash.

### 9b — Notion create + dedupe + `create_and_enrich` + in-flight guard + `--add` CLI (no Slack)

- [x] `NotionClient.create_entry(title)` (`POST /v1/pages`, parent
      `{"type":"data_source_id","data_source_id":…}`, Title + `Status=pending`) — **the one new
      Notion API shape** (spike 01 proved only query + PATCH); live-check it first.
      `NotionClient.find_by_title(title)` (title-`contains` filter, no status filter, client-side
      case-insensitive exact match).
      → `notion.py`. `create_entry` reuses `enrichment_properties(status="pending")` for the
      idempotent shape (phase 2/3). `find_by_title` requires case-insensitive **exact** equality
      after the `contains` prefilter, so *Dune* ≠ *Dune: Part Two* (ADR 0012 dedupe scope). The
      `data_source_id` parent is the sole un-spiked shape → **owner live-check first.**
- [x] `Runtime._inflight: set[str]`; `_sweep` filters out any `page_id in self._inflight`.
      `Runtime.find_duplicate(text)` → `find_by_title`. `Runtime.create_and_enrich(title, *,
      channel, user)`: create page → add `page_id` to `_inflight` → `ainvoke({"page_id",
      "origin":"slack", "notify_channel", "notify_user"})` **without** the single-flight lock →
      `finally` discard from `_inflight`. Factor the `_run_one` interrupt handling into a shared
      helper (write `awaiting_input` + call the picker notifier) so a paused `/add` behaves like
      a swept one.
      → `app.py`. The `_inflight` discard is in a `finally` wrapping the *whole* invoke +
      pause-handling, so the row's terminal/`awaiting_input` write lands **before** it leaves
      the guard — closing the window where the sweep could see it still `pending`. Shared
      `_handle_pause(entry, final_state)` used by both `_run_one` and `create_and_enrich`.
- [x] `--add "<title>"` CLI drives `create_and_enrich` without Slack (stands in for the slash
      command, as `--enrich`/`--resume` do for earlier phases); prints the outcome.
      → `__main__.py` `--add TITLE`: dedupe → create+enrich → print `done`/`failed`, or the
      `--resume` hint when it pauses. No Slack (unbound notifier → no ping / picker).
- [x] **Verify (offline, stubbed clients):** dedupe hit → creates nothing; create→enrich runs
      out-of-band → `done` with `Type` backfilled; a concurrent `_sweep` skips a `page_id` in
      `_inflight`; a simulated crash (page left in `_inflight`, row still `pending`) → a fresh
      sweep reclaims it; a paused `/add` writes `awaiting_input`.
      → **Offline-proven** (same script): dedupe hit → `create_entry` not called + variant
      rejected; create→enrich → `done`, `Type=Movie`, `_inflight` empty after return; a
      page manually held in `_inflight` → sweep `total=0`, row untouched (crash-safe: a fresh
      process has an empty set → the still-`pending` row is reclaimed); an ambiguous `/add` →
      `awaiting_input` + picker notifier fired + `_inflight` empty after the pause.

### 9c — Slack `/add` slash command transport + completion-notifier binding

- [x] Register `@app.command("/add")`: empty text → usage hint; else `ack("Adding *X*…")` (≤3 s
      ephemeral) then spawn a tracked background task → `find_duplicate` (post "already on your
      watchlist (status: …)" and stop) else `create_and_enrich` in a try/except (failure reply
      on error). Completion / not-found feedback comes from the graph `notify` node.
      → `slack.py`. Background tasks held in `self._tasks` (strong refs; discarded on done).
      `_add_flow` posts everything via `chat_postMessage` (never the expiring `response_url`).
      A missing `channel_id` (defensive) short-circuits the ack. **Completion channel decision
      of record:** the `/add` channel; the picker stays fixed to `#notion-movie-db` (verbatim
      reuse).
- [x] `_serve` binds the `CompletionNotifier` to `slack.post_completion` after wiring the picker
      notifier (`Runtime.bind_completion_notifier`).
      → `__main__.py`. `post_completion` suppresses unfurls so the IMDb link stays compact.
- [x] **Verify:** offline — command handler acks + spawns; dedupe reply posted (client mocked);
      completion notifier posts a done/failed message on terminal. **Live (owner-run):**
      `/add Dune` → row created → enriched → completion ping with IMDb/RT/genre; `/add` an
      ambiguous title → picker in `#notion-movie-db` → click → ping after resume; `/add` an
      existing Entry → dedupe reply, no new row; `/add` gibberish → candidate-less picker, then
      failed → not-found ping after the (shortened) timeout; a cron sweep mid-`/add` → sweep
      skips that `page_id` (no double enrichment). Fixture: a Slack `command` payload.
      → **Offline-proven** (`make check` clean; scratchpad script, Slack client mocked):
      `SlackTransport` constructs with fake tokens + registers `/add`; a new title →
      `create_and_enrich` called, no immediate post (ping is the `notify` node); a dedupe hit →
      reply naming the status, `create_and_enrich` **not** called; a create failure → a
      best-effort error reply, no crash; `post_completion` posts to the channel with unfurls
      off. Fixture captured: `tests/fixtures/slack_add_command.json` (documented `command`
      shape). **Live is owner-run** (Slack tokens + the `/add` command registered for Socket
      Mode aren't in Claude's shell env): `--serve` with tokens set → run the five `/add`
      scenarios above; a genuinely nonexistent title escalates to the candidate-less picker and
      only pings `failed` once the (shortened) `STALE_INTERRUPT_TIMEOUT_SECONDS` fires.

---

## Phase 10 — Deploy (local docker compose, always-on)

Goal: the only Docker phase. ADR 0009 / 0007.

**Decisions of record (this phase):**
- **The always-on process is `--serve`** (in-process cron + Slack Socket Mode). No inbound HTTP
  (Socket Mode is an *outbound* WebSocket, ADR 0009) → the compose service exposes **no ports**.
- **Secret injection = entrypoint bridge, not `secrets_dir`.** Compose mounts secrets as
  *files* at `/run/secrets/<NAME>`; `entrypoint.sh` reads each and `export`s it as an env var
  before exec-ing the app. This is chosen over pydantic's `secrets_dir` because LangChain /
  LangSmith read `LANGSMITH_*` **directly from `os.environ`** (bypassing the `Settings` object,
  see `config.py`), so the value must reach the env, not just the typed settings — the bridge
  reproduces exactly what `load_dotenv` does from `.env` in dev, with **zero app-code changes**
  (Phase 10 stays a pure deploy concern).
- **`.env` stays the single source of truth in dev.** `make secrets` (`scripts/gen-secrets.sh`)
  *projects* the 9 secret keys out of `.env` into `secrets/*` (one file each, `printf` — no
  trailing newline; `$(cat)` in the entrypoint strips it anyway). `secrets/` is gitignored.
  Non-secret config (intervals, RPMs, models, LangSmith endpoint/project, data-source id) rides
  in via compose `environment:` with `${VAR:-default}` (Compose auto-reads `.env` for interp).
- **The LLM endpoint is on the host.** `localhost:8888` inside the container is the container
  itself, so compose rewrites `OPENAI_BASE_URL` → `http://host.docker.internal:8888/v1` and adds
  `extra_hosts: ["host.docker.internal:host-gateway"]` (auto on Docker Desktop; needed on Linux).
- **Volume ownership:** the image creates `/data` owned by the non-root `appuser`; Docker seeds a
  fresh named volume from the image path's ownership on first mount, so the checkpointer can write.
- **`.dockerignore` excludes the local `checkpoints.sqlite`** (~40MB) — it must not bloat the
  image or shadow the `/data` volume; the DB is created fresh on the volume at runtime.

- [x] `Dockerfile` (uv-based) + `docker-compose.yml` with `restart: always`.
      → Multi-stage uv build (`ghcr.io/astral-sh/uv:python3.13-bookworm-slim` → `python:3.13-slim`
      runtime), `uv sync --frozen --no-dev` from `uv.lock` (reproducible; dep layer cached
      separately from `src/`), non-root `appuser`, venv on `PATH` (no `uv run` at runtime).
      `ENTRYPOINT ["./entrypoint.sh"]` → the secret bridge → `python -m notion_db_updater --serve`.
      `docker-compose.yml`: single `agent` service, `restart: always`, **no ports**, `extra_hosts`
      for the host LLM. `.dockerignore` keeps the context small + secret-free.
- [x] **Compose secrets** (file-mounted) for all keys; no secrets in the image/compose file.
      → 9 file-mounted secrets (`NOTION_MOVIE_DB_TOKEN`, `OMDB_API_KEY`, `FIRECRAWL/TAVILY/EXA_API_KEY`,
      `OPENAI_API_KEY`, `SLACK_BOT/APP_TOKEN`, `LANGSMITH_API_KEY`) sourced from `./secrets/*`
      (the compose file holds only *paths*, never values). `entrypoint.sh` exports them; the
      secret NAME == the env var the code reads. `make secrets` generates the files from `.env`.
- [x] **Named volume** for the SQLite checkpointer `.sqlite`.
      → `checkpoint-data:/data`; `CHECKPOINT_DB_PATH=/data/checkpoints.sqlite`. Named (not bind)
      so it outlives container recreation → a paused HITL graph resumes after restart (ADR 0007).

**Verification:** `docker compose up`; cron fires on schedule; kill the container → restarts;
an `awaiting_input` graph **survives the restart** (volume persistence proves ADR 0007).
→ **Build + config proven by Claude** (no creds needed): `docker compose config` validates the
merged spec (9 secrets, named volume, no ports, host-gateway) and `docker build` builds the image
clean to the `--serve` entrypoint. **Live `docker compose up` is owner-run** (secrets/creds +
host LLM aren't in Claude's shell env): `make secrets && docker compose up --build` → cron logs
fire; `docker kill` → `restart: always` recovers; create an ambiguous Entry → `awaiting_input`
→ `docker compose restart` mid-wait → the volume-backed `.sqlite` resumes the paused graph.

---

## Cross-cutting verification (end-to-end, once phases land)

1. Add a fresh Entry to the Watchlist → next reconcile enriches it (IMDb + RT + plot + genre)
   and sets `done`; LangSmith trace shows the full graph.
2. Add an ambiguous Entry → pre-filter unsure → Slack picker in `#notion-movie-db` → click →
   row completes; restart mid-wait proves durability.
3. Add a non-existent Entry → `failed`, no RT chain burned, no Slack ping.
4. Re-run reconcile → idempotent (no duplicate writes; only pending/stragglers reprocessed).

---

## Open setup items (config the owner provides; not design questions — RESEARCH §12)

- Slack app-level token `xapp-…` (`connections:write`); enable Socket Mode + Interactivity.
- Slack **slash command `/add`** registered in the app config (phase 9; delivered over the
  same Socket Mode socket — no Request URL needed).
- Local LLM endpoint + the three `OPENAI_*_MODEL` values (each must support tool-calling;
  verified by the phase-0 structured-output spike).
- `LANGSMITH_API_KEY` (tracing on; APAC endpoint; project `NotionMovieDBAgent`).
- OMDb / Firecrawl / Tavily / Exa API keys (Perplexity dropped — ADR 0003 amendment).

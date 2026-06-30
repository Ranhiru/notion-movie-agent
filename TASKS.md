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

## Phase 5 — EnrichedEntry schema + LLM-as-judge

Goal: formalize the output contract and add the wrong-match guard. **Confidence trace-only.**
ADR 0008.

- [ ] Pydantic `EnrichedEntry` (per §5) as the graph's output contract.
- [ ] **Surface both lanes' *identity* into the assembled record — the Judge needs full
      context, not just numbers** (ADR 0003 / 0008). Phase 4's RT lane currently returns only
      `{rt_critic, rt_audience}` and *discards* the matched page (`search_rt` computes the RT
      hit's URL but drops it). Before the Judge can work:
  - [ ] **RT lane:** carry the matched page reference up — add `rt_url` / `rt_title` (+ RT
        page `rt_year` when shown) to `RTState` *and* the parent `EnrichmentState` (shared
        keys, so they merge back); have `firecrawl.search_rt()` return the hit, not just its
        markdown.
  - [ ] **OMDb lane:** ensure the resolved identity (`imdb_id`, title, **year** — add a year
        field to the `Candidate`/details mapping) is in state for the Judge to compare against.
  - [ ] Rationale: the two lanes resolve identity **independently with no shared key**, so a
        title mismatch (*Orphan Black* 2013 vs *Orphan Black: Echoes* 2024) is invisible to a
        scores-only Judge. Title-level matching needs both identities present.
- [ ] **LLM-as-judge fan-in node** (`OPENAI_JUDGE_MODEL`, `with_structured_output`): inspects
      the assembled record (OMDb identity + fields ‖ winning RT identity + scores) for
      wrong-match anomalies — **(a) cross-lane title/year mismatch** (RT page ≠ OMDb title) and
      **(b) score implausibility** (IMDb 9.1 vs RT 18%, plot/title mismatch) → emits
      `confidence` ∈ {high, medium, low}. Heuristic backstop, not a deterministic join.
- [ ] **Confidence stays in graph state / LangSmith only** — NOT written to Notion (no §8
      schema change). Document how to find low-confidence rows via traces.

**Verification:** force a bad `imdbID` → Judge returns low confidence / wrong-match flag; point
the RT lane at a *different* title than OMDb resolved → Judge flags the cross-lane mismatch (the
case scores-only could not catch); a clean match → high. Confirm confidence appears in the
LangSmith trace, not in Notion. Fixture: a deliberately-wrong assembled record (mismatched
OMDb title vs RT page).

---

## Phase 6 — HITL disambiguation (split 6a–6d)

Goal: conditional routing + `interrupt()` + durable resume + Slack. ADR 0006 / 0007 / 0010.

- [ ] **6a — checkpointer + many-candidate path + pre-filter (auto-pick, no human):**
  - [ ] Compile graph with `AsyncSqliteSaver` on a `.sqlite` file, `thread_id = page_id`.
  - [ ] Switch OMDb resolution to surface the **full candidate list** (the phase-2 TODO).
  - [ ] LLM **disambiguation pre-filter** (`OPENAI_DISAMBIGUATION_MODEL`) picks the best
        Candidate; `add_conditional_edges` route that *always* takes the pick for now.
  - [ ] **Verify:** a multi-candidate Entry (e.g. *Dune*) resolves to the pre-filter's choice;
        checkpoint rows appear in the `.sqlite` file.
- [ ] **6b — interrupt() + programmatic resume + restart survival:**
  - [ ] When the pre-filter is unsure → `interrupt()`; set `awaiting_input`; reconcile
        "moves on" to the next Entry.
  - [ ] Resume via `graph.invoke(Command(resume=<imdbID>), thread_id=page_id)` from a test
        harness (no Slack yet).
  - [ ] **Verify:** ambiguous row pauses (`awaiting_input`, `invoke` returns); manual resume
        completes it; **kill + restart the process between interrupt and resume → resume still
        works** (durable execution — the core learning target).
- [ ] **6c — Slack Bolt Socket Mode transport:**
  - [ ] Block Kit picker (≤5 candidates: title + plot + poster; ≤5 buttons; `value` encodes
        `page_id` + `imdbID`) posted to `#notion-movie-db`.
  - [ ] Action handler → `graph.invoke(Command(resume=imdbID), thread_id=page_id)` (resume does
        NOT take the single-flight lock — coordination is by status, ADR 0006).
  - [ ] Wire `@movie-bot run` → `reconcile()` (single-flight).
  - [ ] **Verify:** a real Slack click resolves a real `awaiting_input` row; double-click is a
        safe no-op (finished-thread resume).
- [ ] **6d — 7-day stale-interrupt auto-resolve:** cron finds `awaiting_input` older than 7
      days → resume with the stored pre-filter best-guess + `confidence: low`.
  - [ ] **Verify:** with a shortened timeout, an unclicked row auto-resolves to `done`/low.

---

## Phase 7 — Resilience

Goal: make the sweep robust. ADR 0001 / 0004 / 0007. (LangSmith already on since phase 1.)

- [ ] Per-node `RetryPolicy` (exp backoff + jitter; retries transient 429/5xx, NOT
      `ValueError`).
- [ ] Remaining per-API limiters (`aiolimiter`): Firecrawl ~10 RPM, Tavily credit-aware;
      process-global, shared by sweep + HITL resume. (Notion limiter landed in phase 3.)
- [ ] `InMemoryRateLimiter` on judge/extraction models; confirm `max_concurrency` cap.
- [ ] Checkpoint-per-item so one bad Entry doesn't roll back the batch.

**Verification:** induce/stub a 429 → retry + backoff, not a crash; confirm limiter caps RPS;
LangSmith shows per-node traces + retries.

---

## Phase 8 — Search fallbacks (Tavily → Exa → Perplexity)

Goal: complete the RT ordered chain. ADR 0003.

- [ ] Add Tavily, Exa, Perplexity provider nodes behind Firecrawl, driven by
      `SEARCH_PROVIDERS` config; each runs the same LLM extraction.
- [ ] Chain logic: advance on hard fail OR soft miss; retry transient blips within a provider
      first; **first score wins** (short-circuit the rest).

**Verification:** force Firecrawl to soft-miss → Tavily runs next; confirm first-score-wins
short-circuits remaining providers. Fixtures per provider.

---

## Phase 9 — Slack `/add` second entry point (create-then-enrich, out-of-band)

Goal: a `/add <name>` slash command that originates an Entry from Slack, runs the same
search/disambiguation/enrichment, and reports back. **Additional** entry point — the
Notion-origin path is unchanged. ADR 0012 (reuses 0006 / 0010 / 0001 / 0004).

- [ ] **Slash command transport:** register `/add` in the Slack app; confirm Socket Mode
      delivers `command` events. Handler **`ack()`s within 3 s** (ephemeral "Adding *X*…"),
      then does all work in a background task.
- [ ] **Pre-create dedupe:** query the Watchlist for an existing Entry matching the typed
      string (case-insensitive). On a hit → reply "already in your watchlist (status: …)",
      create nothing.
- [ ] **Create page:** `POST` a new Watchlist page (Entry only, `Type` blank, `Enrichment
      Status = pending`) → `page_id`; idempotent shape reused from phase 2/3.
- [ ] **Origin in graph state:** add `origin` ∈ {`sweep`, `slack`} + Slack notify context
      (channel/user) to the graph state; default `sweep`. Slack path sets `slack` + context.
- [ ] **Out-of-band enrich + in-flight guard:** add `page_id` to a process-global in-flight
      set; `graph.invoke(thread_id=page_id)` **without** the single-flight lock; remove on
      return. The reconcile sweep **skips** any `page_id` in the set (crash → set lost, row
      still `pending` → cron reclaims; ADR 0004 self-heal preserved).
- [ ] **Type backfill:** search OMDb unfiltered; on resolution, backfill the Notion `Type`
      select from `media_type` (agent now writes `Type` for `slack`-origin rows).
- [ ] **Disambiguation reuse:** unsure → existing HITL picker to `#notion-movie-db`, verbatim
      (button `value` = `page_id` + `imdbID`; resume identical). No new picker code.
- [ ] **Terminal notify node:** add a final node on both `done` and `failed` paths; if
      `origin == 'slack'` post the completion (or not-found) message via **`chat_postMessage`**
      (not the slash `response_url` — it expires), else no-op. Fires on the initial run, the
      Slack-click resume, and the 7-day cron auto-resolve alike.

**Verification:** `/add Dune` → row created → enriched → completion ping with IMDb/RT/genre;
`/add` an ambiguous title → picker in `#notion-movie-db` → click → ping fires after resume;
`/add` a known existing Entry → dedupe reply, no new row; `/add` gibberish → not-found ping,
row `failed`; trigger a cron sweep while an `/add` enrich is in flight → sweep skips that
`page_id` (no double enrichment). Fixture: a Slack `command` payload.

---

## Phase 10 — Deploy (local docker compose, always-on)

Goal: the only Docker phase. ADR 0009 / 0007.

- [ ] `Dockerfile` (uv-based) + `docker-compose.yml` with `restart: always`.
- [ ] **Compose secrets** (file-mounted) for all keys; no secrets in the image/compose file.
- [ ] **Named volume** for the SQLite checkpointer `.sqlite`.

**Verification:** `docker compose up`; cron fires on schedule; kill the container → restarts;
an `awaiting_input` graph **survives the restart** (volume persistence proves ADR 0007).

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
- OMDb / Firecrawl / Tavily / Exa / Perplexity API keys.

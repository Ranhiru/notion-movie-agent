# RESEARCH.md — Notion Movie/TV Enrichment Agent

> **Status:** designed (grilled) — ready to build
> **Owner:** Jude
> **Last updated:** 2026-06-27
> **Decisions of record:** `CONTEXT.md` (glossary) + `docs/adr/0001`–`0009`. This file is
> the narrative plan; the ADRs are the authoritative rationale for each decision.

## 0. Framing: a learning project, built local-first

The point is to **understand LangGraph properly** — its core primitives plus several
advanced features — while shipping something real against a personal Notion watchlist.

- **Learn:** fan-out/fan-in, `with_structured_output`, **conditional edges**, **subgraphs**,
  **`interrupt()` + checkpointer (HITL)**, and **LLM-as-judge**.
- **Build local-first:** runs in a **local Docker container**, triggered by an in-process
  **cron** (no inbound HTTP). [ADR 0009]
- **Deploy last & local:** the final phase is just `docker compose up` with `restart: always`
  on an always-on machine — no public host, no Notion webhook (Slack Socket Mode + the hourly
  cron cover everything).

Security shortcuts taken for this learning build (webhook signature verification skipped,
etc.) are recorded as conscious trade-offs in the ADRs, each with a note on what a
production version requires.

## 1. Goal

When a movie/TV row is added to the Notion **Watchlist** database, automatically fill in
its **IMDb rating**, **Rotten Tomatoes critic + audience scores**, **plot summary**, and
**genre**, then write them back to the row.

## 2. Decided approach

- **Framework:** plain **LangGraph** (not Deep Agents) — fixed, known control flow; cheaper,
  more debuggable, and it teaches the primitives Deep Agents is built on. [ADR 0002]
- **Two lanes, not three** (OMDb is authoritative-structured; the search providers form one
  ordered RT chain): [ADR 0003]

```
  read_page
     ▼
  resolve_title → OMDb SEARCH (?s=)
     ▼
  disambiguate (LLM pre-filter, Haiku 4.5)
     ├─ confident ──────────────────────────────►  proceed
     └─ unsure ─ interrupt() ─► Slack (Socket Mode) ─► resume on click
                 │  status=awaiting_input; 7-day timeout → best guess + low conf
     ▼ (conditional edges)
     ├──────────────────────┬──────────────────────────────┐   (fan-out)
     ▼                       ▼
  OMDb lane              RT resolution SUBGRAPH (ordered chain):
  (IMDb, plot, genre)    Firecrawl /search → Tavily → Exa → Perplexity
                         each: LLM extraction → {rt_critic, rt_audience}
                         advance on hard fail OR soft miss; first score wins
     └──────────────────────┴──────────────────────────────┘   (fan-in)
     ▼
  assemble (deterministic) → LLM-as-judge (confidence + wrong-match flag)
     ▼
  update_notion (idempotent upsert by page_id)
```

### Learning targets → LangGraph features

| Goal | LangGraph feature | Where |
|---|---|---|
| Run lanes concurrently | **Parallel nodes (fan-out/fan-in)** | OMDb ‖ RT-resolution |
| Parse RT out of scraped text | **`with_structured_output`** | per RT provider [ADR 0008] |
| Route on a model's judgment | **Conditional edges** | disambiguation → proceed / HITL [ADR 0008] |
| Wait for a human, survive restarts | **`interrupt()` + checkpointer** | disambiguation HITL [ADR 0006/0007] |
| Reusable RT logic | **Subgraph** | RT resolution chain [ADR 0003] |
| Catch wrong matches | **LLM-as-judge** | fan-in confidence node [ADR 0008] |
| Resume instead of restart | **Durable execution** | `AsyncSqliteSaver` [ADR 0007] |

> **Note on the "LLM merge":** the original "reconcile 3 conflicting sources" job is gone —
> OMDb is authoritative structured JSON and the RT chain yields a single winner. The LLM
> work moved to where it's real: **extraction** (per provider), **disambiguation** (pre-filter
> + conditional routing), and **judging** (confidence / wrong-match). [ADR 0008]

## 3. Execution model: everything is a reconcile

One entrypoint, **`reconcile()`**: query the Watchlist for rows still needing work, run each
through the graph. [ADR 0001]

- **Triggers: an hourly in-process cron, plus a manual `@movie-bot run` in Slack.** The Notion
  webhook is **dropped** — Socket Mode + the cron cover everything; it was only ever a latency
  optimization. [ADR 0009/0010/0005]
- **Single-flight lock** (in-process `asyncio.Lock`): only one reconcile at a time. Extra
  triggers **ack/log/drop**; the next cron picks up stragglers. The point is to funnel all
  work through one sequential, concurrency-capped sweep so bursts can't fan out into 429
  storms. [ADR 0001/0002]
- **First reconcile = the ~100-item backfill.** Same code path, just more rows. Process with
  low `max_concurrency` (3–5). [ADR 0001]

### Status lifecycle (the self-healing contract) [ADR 0004/0006]

`Enrichment Status` (a Notion `select`): `pending` → `awaiting_input` → `done` | `failed`.
The reconcile query picks up only **empty or `pending`** rows.

| Status | Meaning |
|---|---|
| `pending` | new row; **or** a run that didn't finish (crash, or transient OMDb error after `RetryPolicy`) — left pending so the cron retries |
| `awaiting_input` | graph interrupted, waiting on a human Slack pick; auto-resolves after 7 days with the LLM's best guess at `confidence: low` |
| `done` | OMDb resolved (IMDb rating + plot present) and the RT cascade ran. **RT is best-effort** and may be null |
| `failed` | OMDb **definitively** could not resolve the title (a real "not found", not a transient error) |

**IMDb gates the status; RT never does.** Always write whatever partial data was obtained
before setting status (idempotent upsert by `page_id`).

## 4. The data sources

- **OMDb lane (metadata).** IMDb rating, IMDb ID, plot (`fullPlot=true`), genre. Resolution
  uses OMDb **search** (`?s=`) to surface candidates, then `?i=<imdbID>` for full details.
  **Resolution logic:** exactly 1 result → proceed (trust it; the **Judge** catches a wrong
  match — we don't disambiguate the common case); multiple → LLM disambiguation; **0 results →
  `failed`** immediately (definitive not-found; don't burn the RT chain or a Slack ping). If
  `Type` is set, pass `type=movie|series` to narrow; if blank, search unfiltered and let the
  1/many logic sort it. OMDb is unreliable for RT (none for TV; often `N/A`) — hence the RT lane.
- **RT resolution (ordered fallback chain, subgraph).** `Firecrawl /search` (scoped to
  `rottentomatoes.com`, with content scrape) is the **primary**; `Tavily → Exa → Perplexity`
  are fallbacks, **active by default**. Each provider runs LLM extraction → `{rt_critic,
  rt_audience}`. A provider **succeeds** if it returns ≥1 usable score; the chain **advances
  on hard failure OR soft miss**, retries transient blips within a provider first, and
  short-circuits at the first score. Two best-effort fields (critic = Tomatometer, audience =
  Popcornmeter) — never conflated. [ADR 0003]

## 5. LLM nodes

**Endpoint: a configurable OpenAI-compatible base URL** via `langchain-openai` —
`ChatOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, model=…).with_structured_output(...)`.
The base URL is configurable so the same code runs against **OpenRouter** *or* a **local LLM**
in dev (e.g. `http://localhost:8888/v1`). **Three configurable models, one per role** (cheap
extraction, stronger judge; swap via config, not code): [ADR 0008/0011]

| Setting | Env var |
|---|---|
| Endpoint / key | `OPENAI_BASE_URL`, `OPENAI_API_KEY` |
| RT extraction | `OPENAI_EXTRACTION_MODEL` |
| Disambiguation | `OPENAI_DISAMBIGUATION_MODEL` |
| LLM-as-judge | `OPENAI_JUDGE_MODEL` |

> **Constraint:** every node uses `with_structured_output`, so each configured model **must
> support structured outputs / tool-calling** at the chosen endpoint.

1. **Per-provider RT extraction** — parse provider output → `{rt_critic, rt_audience}`; also
   drives the chain's fall-through.
2. **Disambiguation pre-filter + conditional routing** — LLM picks the best Candidate; an
   `add_conditional_edges` route proceeds when confident, or `interrupt()`s to Slack when not.
3. **LLM-as-judge (fan-in)** — inspects the assembled record for wrong-match anomalies (e.g.
   IMDb 9.1 vs RT 18%, plot/title mismatch) and emits `confidence`. The guard against
   confidently enriching the **wrong** title.

### Output schema (Pydantic)

```python
class EnrichedEntry(BaseModel):
    title: str
    year: int | None
    media_type: Literal["movie", "tv"]
    imdb_id: str | None
    imdb_rating: float | None        # 0–10
    rt_critic: int | None            # 0–100, Tomatometer
    rt_audience: int | None          # 0–100, Popcornmeter
    plot: str | None
    genre: str | None                # comma-separated, from OMDb
    confidence: Literal["high", "medium", "low"]   # set by the Judge
    sources_used: list[str]
```

## 6. Human-in-the-loop (HITL)

Transport: **Slack Bolt in Socket Mode** — one outbound WebSocket, the app's *only* inbound
path (no public endpoint, works in local Docker). Needs `SLACK_BOT_TOKEN` (`xoxb-…`) **and**
`SLACK_APP_TOKEN` (`xapp-…`, scope `connections:write`); Socket Mode + Interactivity enabled.
It also serves the **manual run**: `@movie-bot run` → `reconcile()` (single-flight). [ADR 0010]

When OMDb search is ambiguous and the LLM pre-filter is unsure, the graph `interrupt()`s,
sets `awaiting_input`, posts the candidates to **`#notion-movie-db`**, and **moves on** to the
next row. The message is a **Block Kit** picker: up to 5 candidates, each a section with title +
plot + poster image (OMDb `Poster`), and up to 5 buttons. Each button `value` encodes
`page_id` + chosen `imdbID`; the click resumes out-of-band via
`graph.invoke(Command(resume=<imdbID>), thread_id=page_id)`. [ADR 0006/0009/0010]

- The resume **does not take the single-flight lock** — coordination is by status (`pending`
  = sweep's domain, `awaiting_input` = resume's domain); the only shared resource (API budget)
  is governed by per-API rate limiters. [ADR 0006]
- **Never click within 7 days?** The cron auto-resolves with the pre-filter's stored best
  guess and marks `done` / `confidence: low`. No title is ever stuck or lost. [ADR 0006]

## 7. Durable execution

`AsyncSqliteSaver` — a single `.sqlite` file on a persistent volume, keyed by `thread_id =
page_id`. Not a separate DB server; it's the file that makes `interrupt()`/resume survive a
multi-hour wait *and* a process restart. Notion's `Enrichment Status` owns **lifecycle** state;
the checkpoint file owns **execution** state — complementary, not redundant. [ADR 0007]

## 8. Notion data model (built)

**Data source:** `Watchlist`, id `ffcdcd68-0449-461d-be8e-0af9b71f9d5f`. API version
**`2025-09-03`** (databases were split into databases + data sources; query
`POST /v1/data_sources/{id}/query`). Token: env `NOTION_MOVIE_DB_TOKEN`. The integration has
access to this one database only (least privilege).

| Property | Type | Filled by | Maps to |
|---|---|---|---|
| Title | title | **you** | `title` (input) |
| Type | select [Movie, TV Show] | **you** | `media_type` (Movie→movie/`type=movie`, TV Show→tv/`type=series`) |
| IMDB Rating | number | agent | `imdb_rating` |
| RT Critic Score | number | agent | `rt_critic` |
| RT Audience Score | number | agent | `rt_audience` |
| Plot Summary | rich_text | agent | `plot` |
| Genre | rich_text | agent | `genre` |
| Enrichment Status | select [pending, awaiting_input, done, failed] | agent | status |
| Status | status [Not started, …] | **you** — untouched | — |
| Watched | checkbox | **you** — untouched | — |

Reconcile filter: `Enrichment Status` `is_empty` **OR** `equals pending`.

## 9. Rate limiting & resilience

The framework throttles the LLM and caps concurrency but does **not** rate-limit your external
APIs — you must. [unchanged from prior research]

- **Per-API limiters you build** (`aiolimiter` / `asyncio.Semaphore`): **Notion ≤3 rps**
  (honor `Retry-After` on 429), **Firecrawl ~10 RPM** (low tier), Tavily credit-aware. These
  are process-global, shared by the sweep and any HITL resume.
- **Framework gives you:** `InMemoryRateLimiter` on the merge/judge model; `max_concurrency`
  (3–5); per-node `RetryPolicy` (exp backoff + jitter; retries transient 429/5xx, not
  `ValueError`) — the right place to handle transient OMDb/Firecrawl errors before failing.
- **Checkpoint per item** so one bad title doesn't roll back the batch; **idempotent writes**
  (upsert by `page_id`) so re-runs are safe.

## 10. Security (learning-build posture; production TODOs noted)

- **Webhook verification:** N/A — the webhook is dropped. The app's only inbound path is the
  Slack socket (outbound-initiated). [ADR 0005/0009/0010]
- **Secrets:** **`.env`** in local dev; **Docker Compose secrets** (file-mounted) for the
  compose deployment. Never committed; ship a `.env.example` with empty keys. Keys:
  `NOTION_MOVIE_DB_TOKEN`, OMDb, Firecrawl, Tavily, Exa, Perplexity, `OPENAI_API_KEY`,
  `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `LANGSMITH_API_KEY`. Plus config: `OPENAI_BASE_URL`,
  `OPENAI_EXTRACTION_MODEL` / `OPENAI_DISAMBIGUATION_MODEL` / `OPENAI_JUDGE_MODEL`,
  `SEARCH_PROVIDERS`, `LANGSMITH_TRACING=true`,
  `LANGSMITH_ENDPOINT=https://apac.api.smith.langchain.com`,
  `LANGSMITH_PROJECT=NotionMovieDBAgent`.
- **Least privilege:** integration scoped to the Watchlist DB only.
- **Input trust:** treat scraped/searched content as untrusted input to the LLM nodes — it
  fills the schema, it doesn't drive actions.
- **Idempotency:** upsert by `page_id`.

## 11. Revised milestones (local-first; production deploy is the last phase)

1. **Skeleton + local Docker + Notion read.** **uv** project (LangGraph/LangChain **1.x**),
   `Dockerfile` + `docker-compose.yml` (`restart: always`), `.env.example`, one node that reads
   a Watchlist page by id (2025-09-03 API) and prints it. Confirm `data_source_id` + property
   names.
2. **OMDb lane, single source, end-to-end.** `read_page → OMDb → write IMDb rating + plot +
   genre`; idempotent upsert; set `Enrichment Status`. Run one row by hand.
3. **`reconcile()` + status lifecycle + in-process cron.** Query unfilled rows → graph each →
   `done`/`failed`/`pending` per §3; single-flight lock; manual "run now". First run = the
   ~100-item backfill.
4. **Structured output + Judge.** Pydantic `EnrichedEntry`; deterministic assembly; LLM-as-judge
   `confidence` node (`OPENAI_JUDGE_MODEL` via the configurable endpoint). *(structured-output learning)*
5. **RT resolution subgraph + fan-out/fan-in.** Firecrawl `/search` primary, per-provider LLM
   extraction → `rt_critic`/`rt_audience`; OMDb ‖ RT in parallel; assemble → judge. *(core
   fan-out/fan-in + subgraph learning)*
6. **HITL disambiguation.** OMDb `?s=` → LLM pre-filter → conditional edge → `interrupt()` +
   **Slack Socket Mode** → resume; `AsyncSqliteSaver` on a volume; `awaiting_input` + 7-day
   timeout auto-resolve. *(conditional edges + HITL + checkpointer learning)*
7. **Resilience.** Per-node `RetryPolicy`, per-API limiters (`aiolimiter`), checkpoint-per-item,
   observability (**LangSmith** for tracing the graph while learning).
8. **Search fallbacks.** Fill in `Tavily → Exa → Perplexity` behind Firecrawl in the chain
   (`SEARCH_PROVIDERS` config).
9. **Deploy (last phase) — local `docker compose`, always-on.** `docker compose up` with
   `restart: always` and **Compose secrets** (file-mounted), on a machine that stays up so the
   hourly cron fires. Persist the SQLite checkpointer on a named volume. No public host, no
   Notion webhook (Socket Mode + cron suffice). [ADR 0009]

## 12. Remaining setup items (config, not open design questions)

1. **Generate the Slack app-level token** (`xapp-…`, scope `connections:write`) → `SLACK_APP_TOKEN`,
   alongside the existing bot token. Enable Socket Mode + Interactivity in the app config. [ADR 0010]
2. **Pick the three `OPENAI_*_MODEL` values** (and `OPENAI_BASE_URL` — local LLM in dev, or
   OpenRouter); each must support structured outputs / tool-calling. [ADR 0011]
3. **Fill in `LANGSMITH_API_KEY`** (tracing is enabled; APAC endpoint, project `NotionMovieDBAgent`).

## 13. Key references

- **Decisions of record:** `CONTEXT.md`, `docs/adr/0001`–`0011`.
- [LangGraph docs](https://docs.langchain.com/oss/python/) · [persistence](https://langchain-ai.github.io/langgraph/concepts/persistence/) · [HITL](https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/)
- [Notion 2025-09-03 upgrade](https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03) · [query a data source](https://developers.notion.com/reference/query-a-data-source) · [webhook events](https://developers.notion.com/reference/webhooks-events-delivery) · [request limits](https://developers.notion.com/reference/request-limits)
- [OMDb API](http://www.omdbapi.com/) · [Firecrawl /search](https://docs.firecrawl.dev/features/search) · [Firecrawl rate limits](https://docs.firecrawl.dev/rate-limits)
- [Slack Socket Mode](https://api.slack.com/apis/socket-mode) · [Exa vs Tavily](https://exa.ai/versus/tavily) · [Perplexity Sonar pricing](https://docs.perplexity.ai/docs/getting-started/pricing)

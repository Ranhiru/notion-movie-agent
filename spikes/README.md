# Phase 0 — Spikes

Throwaway, timeboxed de-risking of the recent/uncertain APIs before they block a feature
phase. The **code** is throwaway; the **learnings** (version-correct snippets below) and the
**pinned versions** (`pyproject.toml`) carry forward.

All spikes load `../.env` via `python-dotenv` (see `_env.py`). Shell-exported vars win over
`.env` (`override=False`), so you can also just run from a shell that already has the secrets.
Fill `../.env` (copy from `../.env.example`) before running the credential-gated ones.

## Pinned stack (verified against the *installed* versions)

| Package | Version |
|---|---|
| Python | 3.13.11 (uv-managed; `.python-version`) |
| langgraph | 1.2.6 |
| langgraph-checkpoint-sqlite | 3.1.0 |
| langchain | 1.3.11 |
| langchain-core | 1.4.8 |
| langchain-openai | 1.3.3 |
| httpx | 0.28.1 |
| python-dotenv | 1.2.2 |
| slack-bolt | 1.28.0 |
| websocket-client | 1.9.0 |

## Status

| # | Spike | Needs creds | Status |
|---|---|---|---|
| — | `uv` bootstrap + pinned 1.x | no | ✅ done |
| 02 | LangGraph toy graph (fan-out/in, cond. edge, interrupt) | no | ✅ proven |
| 03 | AsyncSqliteSaver + interrupt() restart | no | ✅ proven (across real process restart) |
| 01 | Notion 2025-09-03 data_sources | NOTION_MOVIE_DB_TOKEN | ✅ proven (query + write) |
| 04 | local-LLM structured output | OPENAI_* | ✅ proven (all 3 roles; no OpenRouter needed) |
| 05 | Firecrawl → RT extraction | FIRECRAWL_API_KEY (+OPENAI_*) | ✅ proven (4/4 titles) |
| 06 | Slack Socket Mode | SLACK_BOT_TOKEN, SLACK_APP_TOKEN | ✅ proven (click round-trip) |

**Phase 0 complete — all 7 spikes proven.**

## Run

```bash
# credential-free — proven:
uv run python spikes/02_langgraph_toy_graph.py

uv run python spikes/03_sqlite_interrupt_restart.py reset
uv run python spikes/03_sqlite_interrupt_restart.py interrupt --thread page-123
uv run python spikes/03_sqlite_interrupt_restart.py resume    --thread page-123 --value tt1160419

# credential-gated — run from a shell with the secrets, or after filling ../.env.
# In Claude Code, prefix with `!` so it runs in YOUR fish shell (which has the env):
uv run python spikes/01_notion_data_source.py query              # read-only, SAFE
uv run python spikes/01_notion_data_source.py write --page-id <PAGE_ID>   # mutates agent fields
uv run python spikes/04_local_llm_structured_output.py
uv run python spikes/05_firecrawl_rt_extraction.py
uv run python spikes/06_slack_socket_mode.py                     # long-running; click, then Ctrl-C
```

## Learnings (version-correct snippets)

### LangGraph 1.2.6 — graph primitives
```python
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command, RetryPolicy
from langgraph.checkpoint.memory import InMemorySaver

# reducer fan-in: concurrent writes to the SAME channel merge instead of erroring
from typing import Annotated, TypedDict
import operator
class State(TypedDict):
    lane_results: Annotated[list[str], operator.add]

g = StateGraph(State)
g.add_node("lane", fn, retry_policy=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)))
# fan-out  = 2+ edges out of one node;  fan-in = 2+ edges into one node (waits for all)
g.add_conditional_edges("assemble", router_fn, {"ret_a": "node_a", "ret_b": "node_b"})
graph = g.compile(checkpointer=InMemorySaver())   # interrupt() REQUIRES a checkpointer
```
- `RetryPolicy` is a **NamedTuple**, fields:
  `(initial_interval, backoff_factor, max_interval, max_attempts, jitter, retry_on)`.
- `interrupt(payload)` makes `invoke()` **return** `{"__interrupt__": (Interrupt(value=payload),)}`
  — this is what lets `reconcile()` "move on" to the next Title (ADR 0006).
- Resume: `graph.invoke(Command(resume=<value>), config)`; `config = {"configurable": {"thread_id": ...}}`.
- The `<value>` becomes the **return value of `interrupt()`** where execution paused.

### AsyncSqliteSaver 3.1.0 — durable HITL (the big risk — confirmed working)
```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async with AsyncSqliteSaver.from_conn_string("checkpoints.sqlite") as saver:
    await saver.setup()                       # idempotent; creates tables, sets WAL
    graph = builder.compile(checkpointer=saver)
    await graph.ainvoke({...}, cfg)           # -> returns on interrupt()
    snap = await graph.aget_state(cfg)        # snap.next == pending node(s) after restart
    await graph.ainvoke(Command(resume=v), cfg)
```
- `from_conn_string()` is an **async context manager**. Call `await saver.setup()` after entering.
- **journal_mode = WAL** is set by `setup()` (confirmed) — good for concurrent readers.
- **Proven across a real process kill**: interrupt in process A (exits) → resume in a fresh
  process B sharing the same `.sqlite` → graph completes. `thread_id = page_id` (ADR 0007).

### python-dotenv 1.2.2 — gotcha
- It does **NOT** strip inline `# comments`: `KEY=  # note` sets `KEY` to `"# note"`.
  Keep comments on their own lines in `.env`; leave unset keys as bare `KEY=`.
- `KEY=#value` (no space) keeps the `#` as part of the value — fine for `SLACK_CHANNEL=#chan`.

### Notion 2025-09-03 (CONFIRMED live — spike 01)
- Query: `POST /v1/data_sources/{id}/query`, header `Notion-Version: 2025-09-03`. → 200.
- Reconcile filter (compound `or` over a *select* prop):
  `{"or": [{"property":"Enrichment Status","select":{"is_empty":true}},
           {"property":"Enrichment Status","select":{"equals":"pending"}}]}`
- Write: `PATCH /v1/pages/{page_id}` with property shapes (all round-trip OK)
  `{"IMDB Rating":{"number":8.1}}`, `{"Plot Summary":{"rich_text":[{"text":{"content":"…"}}]}}`,
  `{"Enrichment Status":{"select":{"name":"pending"}}}`.
- **Gotcha:** the Watchlist has blank rows (empty Title) that match `is_empty`. Phase 3
  resolution must skip/`failed` a blank Title — don't search OMDb for `""`.

### langchain-openai 1.3.3 — structured output (CONFIRMED live — spike 04)
```python
from langchain_openai import ChatOpenAI
ChatOpenAI(base_url=..., api_key=..., model=..., temperature=0).with_structured_output(MyModel).invoke(prompt)
```
- Local `Qwen3.6-35B-A3B-MTP-GGUF` @ localhost:8888 parses all 3 role schemas cleanly →
  **no OpenRouter fallback needed** (ADR 0011 stays dormant).

### Firecrawl → RT extraction (CONFIRMED live, 4/4 — spike 05)
- Scrape `POST https://api.firecrawl.dev/v2/scrape` (v1 fallback), `{"formats":["markdown"],
  "onlyMainContent":true}` → markdown contains the scores.
- **Gotcha (cost us 2/4 on first run):** RT scores can sit **~15k chars** into the markdown
  (Dune @ ~10.4k, Last of Us @ ~14.8k). Blind `markdown[:8000]` dropped them. Phase 4 should
  slice to the score region, not a magic char count — and mind the local model's context.
- RT **TV** pages carry per-season scores → a critic/audience split like 94 vs 62 is an
  anomaly the Phase 5 Judge should catch, not a bug.

### Wiring gotchas (for later phases)
- **LangSmith:** `.env` ships `LANGSMITH_TRACING=true` but an empty `LANGSMITH_API_KEY` →
  noisy `401 Unauthorized` on every LLM call. Set the key (intended; tracing ON from Phase 1)
  or `LANGSMITH_TRACING=false` to silence.
- **Slack:** the bot must be invited to the target channel (`/invite @bot`) or
  `chat.postMessage` returns `not_in_channel` even with a valid token.

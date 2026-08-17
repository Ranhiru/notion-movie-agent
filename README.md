# Notion Movie/TV Enrichment Agent

A LangGraph/LangChain service that enriches a Notion **Watchlist** with IMDb rating,
Rotten Tomatoes scores, plot, and genre. 

Design docs: [`CONTEXT.md`](CONTEXT.md) · [`RESEARCH.md`](RESEARCH.md) ·
[`docs/adr/`](docs/adr) · [`TASKS.md`](TASKS.md).


# Preview 

https://github.com/user-attachments/assets/2592c712-a857-41c2-bbc9-78a015647c74

## The enrichment graph

Uses OMDB AI as the authoritative source to find movies, fetches Rotten Tomatoes scores with best effort using Tavily/Exa/Firecrawl.

If there are multiple matches, posts a message on Slack (HITL) asking user to confirm.

Running on OpenRouter with `openai/gpt-5.4-nano` currently in prod, with traces uploaded to LangSmith.


![Enrichment graph](flow.png)

Regenerate the diagram: `uv run python -m notion_db_updater --generate-graph`.

## Running it

Requires `uv`. Copy `.env.example` → `.env` (only `NOTION_MOVIE_DB_TOKEN` is needed to boot).

```bash
uv run python -m notion_db_updater                    # read + print the Watchlist
uv run python -m notion_db_updater --enrich <page_id> # enrich ONE Entry
uv run python -m notion_db_updater --reconcile        # sweep the whole Watchlist once
uv run python -m notion_db_updater --serve            # in-process cron loop
```

## Deploy (Docker Compose, always-on)

Phase 10 (ADR 0009 / 0007) — one long-lived container running `--serve` (in-process cron +
Slack Socket Mode), kept up by `restart: always`. No inbound HTTP (Socket Mode is an outbound
WebSocket), so no ports are exposed.

```bash
make secrets              # project .env's 9 secret keys into secrets/* (gitignored)
docker compose up --build # start the always-on agent
```

- **Secrets** are file-mounted (`docker-compose.yml` → `secrets:`), never baked into the image
  or the compose file. `make secrets` regenerates `secrets/*` from `.env` — rerun after
  rotating a key. `entrypoint.sh` bridges `/run/secrets/*` into env vars on boot.
- **State** (the SQLite checkpointer) lives on the **named volume** `checkpoint-data`, so a
  paused `awaiting_input` HITL run survives a container restart (ADR 0007).
- **Config** — `OPENAI_BASE_URL` and all non-secret tuning (intervals, RPMs, models, LangSmith
  project) are read from `.env`/env via compose `environment:` with sensible defaults. A remote
  LLM endpoint (e.g. OpenRouter) works as-is; for a host-local LLM use
  `http://host.docker.internal:8888/v1` (and on Linux add
  `extra_hosts: ["host.docker.internal:host-gateway"]` to the service).

Verify durability: create an ambiguous Entry → it goes `awaiting_input` → `docker compose
restart` mid-wait → the paused graph resumes from the volume-backed `.sqlite`.

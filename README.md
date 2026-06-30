# Notion Movie/TV Enrichment Agent

A Python + **LangGraph** service that enriches a Notion **Watchlist** with IMDb rating,
Rotten Tomatoes scores, plot, and genre. A learning project for LangGraph primitives.

Design docs: [`CONTEXT.md`](CONTEXT.md) · [`RESEARCH.md`](RESEARCH.md) ·
[`docs/adr/`](docs/adr) · [`TASKS.md`](TASKS.md).

## The enrichment graph

`reconcile()` runs each Watchlist Entry through this graph. The OMDb and Rotten Tomatoes
lanes **fan out in parallel**, then fan in to `assemble` before the write-back:

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

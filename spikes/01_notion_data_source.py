"""Spike 01 — Notion 2025-09-03 data_sources: one real query + one real write.

De-risks Phases 1, 2, 3 at once against the live Watchlist. Proves:

  - the 2025-09-03 query endpoint:  POST /v1/data_sources/{id}/query
  - the reconcile compound filter:  Enrichment Status is_empty OR equals "pending"
    (an `or` over a *select* property)
  - the write payload shapes:        number / rich_text / select  (PATCH /v1/pages/{id})
  - the §8 property names map to what's actually in the database

Needs NOTION_MOVIE_DB_TOKEN. Data source id defaults to the Watchlist
(NOTION_WATCHLIST_DATA_SOURCE_ID).

Run (read-only by default — SAFE):
    uv run python spikes/01_notion_data_source.py query

Verify the write payload shape (MUTATES agent-owned fields on ONE page you choose;
re-running enrichment overwrites them — non-destructive to your own columns):
    uv run python spikes/01_notion_data_source.py write --page-id <PAGE_ID>
"""

from __future__ import annotations

import argparse
import json

import _env  # noqa: F401  (loads .env on import)
import httpx

NOTION_VERSION = "2025-09-03"
API = "https://api.notion.com/v1"

# §8 property names — the contract this spike verifies against the live DB.
PROP_STATUS = "Enrichment Status"  # select [pending, awaiting_input, done, failed]
PROP_IMDB = "IMDB Rating"  # number
PROP_PLOT = "Plot Summary"  # rich_text
PROP_GENRE = "Genre"  # rich_text
PROP_TITLE = "Title"  # title


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _title_text(page: dict) -> str:
    prop = page.get("properties", {}).get(PROP_TITLE, {})
    parts = prop.get("title", [])
    return "".join(p.get("plain_text", "") for p in parts) or "(untitled)"


def _select_name(page: dict, prop: str) -> str | None:
    sel = page.get("properties", {}).get(prop, {}).get("select")
    return sel.get("name") if sel else None


def cmd_query(token: str, data_source_id: str) -> None:
    # The reconcile filter: empty OR pending. This is the exact body Phase 3 will use.
    body = {
        "filter": {
            "or": [
                {"property": PROP_STATUS, "select": {"is_empty": True}},
                {"property": PROP_STATUS, "select": {"equals": "pending"}},
            ]
        },
        "page_size": 5,
    }
    url = f"{API}/data_sources/{data_source_id}/query"
    resp = httpx.post(url, headers=_headers(token), json=body, timeout=30)
    print(f"POST {url} -> {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    print(f"matched {len(results)} row(s) needing enrichment (empty OR pending):\n")
    for page in results:
        print(f"  • {_title_text(page)!r}")
        print(f"      page_id            = {page['id']}")
        print(f"      Type               = {_select_name(page, 'Type')}")
        print(f"      Enrichment Status  = {_select_name(page, PROP_STATUS)}")

    # Confirm the property names actually present (catches §8 drift).
    if results:
        present = sorted(results[0].get("properties", {}).keys())
        print(f"\n  property names on row 0: {present}")
        for expected in (
            PROP_TITLE,
            "Type",
            PROP_IMDB,
            PROP_PLOT,
            PROP_GENRE,
            PROP_STATUS,
        ):
            mark = "✓" if expected in present else "✗ MISSING"
            print(f"      {mark}  {expected!r}")
    print(
        "\n  query spike OK ✓  (capture this JSON as tests/fixtures/notion_query.json in Phase 1)"
    )


def cmd_write(token: str, page_id: str) -> None:
    # Verifies the three serialization shapes the agent writes. Agent-owned fields only.
    props = {
        PROP_IMDB: {"number": 8.1},  # number
        PROP_PLOT: {
            "rich_text": [{"text": {"content": "[spike] plot text"}}]
        },  # rich_text
        PROP_GENRE: {
            "rich_text": [{"text": {"content": "[spike] Drama, Sci-Fi"}}]
        },  # rich_text
        PROP_STATUS: {"select": {"name": "pending"}},  # select
    }
    url = f"{API}/pages/{page_id}"
    print(f"PATCH {url}")
    print("payload properties:\n" + json.dumps(props, indent=2))
    resp = httpx.patch(
        url, headers=_headers(token), json={"properties": props}, timeout=30
    )
    print(f"-> {resp.status_code}")
    resp.raise_for_status()
    page = resp.json()
    print("\nwrite accepted. round-trip read-back:")
    print(f"  {PROP_IMDB:18} = {page['properties'][PROP_IMDB].get('number')}")
    rt = page["properties"][PROP_PLOT].get("rich_text", [])
    print(f"  {PROP_PLOT:18} = {(rt[0]['plain_text'] if rt else None)!r}")
    print(f"  {PROP_STATUS:18} = {_select_name(page, PROP_STATUS)}")
    print("\n  write spike OK ✓  (number / rich_text / select shapes all confirmed)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("query")
    pw = sub.add_parser("write")
    pw.add_argument(
        "--page-id", required=True, help="a Watchlist page id to write test values to"
    )
    args = p.parse_args()

    (token,) = _env.require("NOTION_MOVIE_DB_TOKEN")
    if args.cmd == "query":
        data_source_id = _env.get(
            "NOTION_WATCHLIST_DATA_SOURCE_ID", "ffcdcd68-0449-461d-be8e-0af9b71f9d5f"
        )
        cmd_query(token, data_source_id)
    elif args.cmd == "write":
        cmd_write(token, args.page_id)


if __name__ == "__main__":
    main()

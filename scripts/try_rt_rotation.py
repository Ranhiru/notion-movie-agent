"""Local Phase 8 smoke test — RT provider rotation against the REAL APIs, no Notion.

Runs several titles through build_search_client in ONE process, so the round-robin counter
actually rotates the lead across providers (it resets per process — repeated --enrich won't
show rotation). Prints, per title: the winning provider, the matched RT page, and whether the
extractor pulled real scores from that provider's content (the plaintext-parity check).

Prereqs in .env: FIRECRAWL_API_KEY, TAVILY_API_KEY, EXA_API_KEY, the OPENAI_* extraction model,
and SEARCH_PROVIDERS=firecrawl,tavily,exa (drop perplexity). Then:

    uv run python scripts/try_rt_rotation.py
    uv run python scripts/try_rt_rotation.py "Dune: Part Two" "The Bear" "Parasite"
"""

from __future__ import annotations

import asyncio
import logging
import sys

from notion_db_updater.config import get_settings
from notion_db_updater.llm import extraction_model
from notion_db_updater.providers import build_search_client
from notion_db_updater.rt import extract_rt_page

# INFO so the composite's "rt_search: provider X won for …" lines print.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_TITLES = ["Dune: Part Two", "The Bear", "Parasite", "The Last of Us"]


async def main() -> None:
    titles = sys.argv[1:] or DEFAULT_TITLES
    settings = get_settings()
    llm = extraction_model(settings)
    print(f"SEARCH_PROVIDERS = {settings.search_providers}\n")
    async with build_search_client(settings) as search:
        for title in titles:
            print(f"── {title!r} ─────────────────────────────")
            hits = await search.search_rt_candidates(title)  # winner logged by the composite
            if not hits:
                print("  no RT page (soft miss across all providers)\n")
                continue
            top = hits[0]
            print(f"  page: {top.url}  (year={top.year}, {len(hits)} candidate(s))")
            # Parity check: can the extractor pull scores from THIS provider's content shape?
            page = await extract_rt_page(llm, top.markdown)
            print(f"  extracted: critic={page.rt_critic}  audience={page.rt_audience}")
            got = page.rt_critic is not None or page.rt_audience is not None
            verdict = (
                "✅ scores parsed from provider content"
                if got
                else "⚠️  no scores — check the score markers in this provider's content"
            )
            print(f"  {verdict}\n")


asyncio.run(main())

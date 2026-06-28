"""Spike 05 — Firecrawl -> LLM extracts RT Tomatometer + Popcornmeter.

The RT lane's primary provider is Firecrawl `/search` (ADR 0003 / RESEARCH §4): in
production we start from a *title*, not a URL, and RT slugs aren't derivable from a title
(`/m/parasite_2019`, `/tv/the_last_of_us`). So the real flow DISCOVERS the page:

    search "<title> <year> site:rottentomatoes.com"  -> pick the canonical /m/ or /tv/ hit
    -> use the markdown /search scraped inline (one call) -> LLM extract {rt_critic, rt_audience}

Two modes:
  search  (DEFAULT, the ADR-0003 path)  title -> search+scrape -> extract
  scrape  (isolates extraction)         known RT URL -> scrape -> extract

Both pass maxAge=1 week to Firecrawl's scrape options, so a repeat run within a week is
served from Firecrawl's cache (much faster) instead of re-fetching. (REST `maxAge`, in ms,
is the SDK's `max_age=` param.)

Needs FIRECRAWL_API_KEY. LLM extraction additionally needs OPENAI_BASE_URL/KEY +
OPENAI_EXTRACTION_MODEL; without those it just fetches + saves markdown for eyeballing.

Run:
    uv run python spikes/05_firecrawl_rt_extraction.py            # search mode (default)
    uv run python spikes/05_firecrawl_rt_extraction.py scrape     # known-URL mode
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

import httpx

import _env

OUT = Path(__file__).resolve().parent / "_out"

WEEK_MS = 7 * 24 * 60 * 60 * 1000  # 604_800_000 — maxAge: serve from cache if <1 week old

# search mode: start from a TITLE (what we actually have in the Watchlist), not a URL.
RT_TITLES = [
    ("Dune: Part Two", 2024, "movie"),
    ("The Godfather", 1972, "movie"),
    ("Parasite", 2019, "movie"),
    ("The Last of Us", 2023, "tv"),
]

# scrape mode: known URLs, to isolate the extraction step from discovery.
RT_URLS = [
    "https://www.rottentomatoes.com/m/dune_part_two",
    "https://www.rottentomatoes.com/m/the_godfather",
    "https://www.rottentomatoes.com/m/parasite_2019",
    "https://www.rottentomatoes.com/tv/the_last_of_us",
]


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _bases() -> list[str]:
    base = _env.get("FIRECRAWL_API_URL")  # optional override
    return [base] if base else ["https://api.firecrawl.dev/v2", "https://api.firecrawl.dev/v1"]


def firecrawl_search(api_key: str, query: str, limit: int = 5) -> list[dict]:
    """Search the web, scraping each hit to markdown inline (one call). v2 -> v1 fallback.

    Returns a normalized list of {url, title, markdown}.
    """
    scrape_opts = {"formats": ["markdown"], "onlyMainContent": True, "maxAge": WEEK_MS}
    last_err = None
    for b in _bases():
        body = {"query": query, "limit": limit, "scrapeOptions": scrape_opts}
        if b.endswith("/v2"):
            body["sources"] = ["web"]  # v2 groups results by source; v1 doesn't take this
        try:
            resp = httpx.post(f"{b}/search", headers=_headers(api_key), json=body, timeout=120)
            if resp.status_code == 404:
                last_err = f"404 at {b}"
                continue
            resp.raise_for_status()
            data = resp.json().get("data", {})
            if isinstance(data, dict):  # v2: {"web": [...]}
                hits = data.get("web") or data.get("results") or []
            elif isinstance(data, list):  # v1: flat list
                hits = data
            else:
                hits = []
            return [
                {"url": h.get("url", ""), "title": h.get("title", ""), "markdown": h.get("markdown", "")}
                for h in hits
            ]
        except httpx.HTTPError as e:
            last_err = f"{type(e).__name__} at {b}: {e}"
    raise RuntimeError(f"search failed for {query!r}: {last_err}")


def scrape_markdown(api_key: str, url: str) -> str:
    """Scrape one URL to markdown (maxAge cache). v2 -> v1 fallback."""
    body = {"url": url, "formats": ["markdown"], "onlyMainContent": True, "maxAge": WEEK_MS}
    last_err = None
    for b in _bases():
        try:
            resp = httpx.post(f"{b}/scrape", headers=_headers(api_key), json=body, timeout=120)
            if resp.status_code == 404:
                last_err = f"404 at {b}"
                continue
            resp.raise_for_status()
            md = resp.json().get("data", {}).get("markdown", "")
            if md:
                return md
            last_err = f"no markdown in response from {b}"
        except httpx.HTTPError as e:
            last_err = f"{type(e).__name__} at {b}: {e}"
    raise RuntimeError(f"scrape failed for {url}: {last_err}")


def pick_rt_hit(hits: list[dict]) -> dict | None:
    """Choose the canonical RT title page from search hits.

    Prefer a bare /m/<slug> or /tv/<slug> (2 path segments) over deep links like
    /m/<slug>/reviews; fall back to any rottentomatoes.com /m/ or /tv/ URL.
    """
    rt = []
    for h in hits:
        netloc = urlparse(h["url"]).netloc.lower()
        path = urlparse(h["url"]).path
        if netloc.endswith("rottentomatoes.com") and (path.startswith("/m/") or path.startswith("/tv/")):
            rt.append((path.rstrip("/").count("/") == 2, h))  # canonical first
    if not rt:
        return None
    rt.sort(key=lambda t: not t[0])  # True (canonical) sorts before False
    return rt[0][1]


def extract_scores(markdown: str):
    """LLM extraction (returns None if the LLM isn't configured)."""
    if not (_env.get("OPENAI_BASE_URL") and _env.get("OPENAI_EXTRACTION_MODEL")):
        return None
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel, Field

    class RTScores(BaseModel):
        rt_critic: int | None = Field(None, ge=0, le=100, description="Tomatometer 0-100")
        rt_audience: int | None = Field(None, ge=0, le=100, description="Popcornmeter 0-100")

    llm = ChatOpenAI(
        base_url=_env.get("OPENAI_BASE_URL"),
        api_key=_env.get("OPENAI_API_KEY"),
        model=_env.get("OPENAI_EXTRACTION_MODEL"),
        temperature=0,
    )
    # LEARNING (spike run): RT scores can sit ~15k chars into the markdown (after nav/
    # preamble) — e.g. Dune Part Two @ ~10.4k, The Last of Us @ ~14.8k. An 8k truncation
    # silently dropped them. Use a wide window here; Phase 4 should slice smartly (find the
    # score region) rather than rely on a magic char count + watch the local model's context.
    prompt = (
        "From this Rotten Tomatoes page markdown, extract the critic score (Tomatometer) and "
        "audience score (Popcornmeter) as integers 0-100. Use null if a score is absent.\n\n"
        + markdown[:20000]
    )
    return llm.with_structured_output(RTScores).invoke(prompt)


def _report(md: str, slug: str, llm_on: bool) -> bool:
    path = OUT / f"rt_{slug}.md"
    path.write_text(md)
    print(f"    {len(md):,} chars markdown -> {path.relative_to(OUT.parent.parent)}")
    if not llm_on:
        return False
    scores = extract_scores(md)
    print(f"    extracted: critic={scores.rt_critic}  audience={scores.rt_audience}")
    return scores.rt_critic is not None or scores.rt_audience is not None


def cmd_search(api_key: str, llm_on: bool) -> None:
    ok = 0
    for title, year, media_type in RT_TITLES:
        query = f"{title} {year} site:rottentomatoes.com"
        print(f"• search: {query!r}")
        try:
            hits = firecrawl_search(api_key, query)
        except Exception as e:  # noqa: BLE001
            print(f"    search FAILED: {e}\n")
            continue
        hit = pick_rt_hit(hits)
        if not hit:
            hosts = [urlparse(h["url"]).netloc for h in hits]
            print(f"    no RT page in {len(hits)} hits (got: {hosts}) — soft miss\n")
            continue
        print(f"    picked: {hit['url']}")
        md = hit["markdown"] or scrape_markdown(api_key, hit["url"])  # fallback if not inline
        slug = urlparse(hit["url"]).path.rstrip("/").split("/")[-1]
        if _report(md, slug, llm_on):
            ok += 1
        print()
    _summary("search", ok, len(RT_TITLES), llm_on)


def cmd_scrape(api_key: str, llm_on: bool) -> None:
    ok = 0
    for url in RT_URLS:
        print(f"• scrape: {url}")
        try:
            md = scrape_markdown(api_key, url)
        except Exception as e:  # noqa: BLE001
            print(f"    scrape FAILED: {e}\n")
            continue
        if _report(md, url.rstrip("/").split("/")[-1], llm_on):
            ok += 1
        print()
    _summary("scrape", ok, len(RT_URLS), llm_on)


def _summary(mode: str, ok: int, total: int, llm_on: bool) -> None:
    if llm_on:
        print(f"firecrawl {mode} spike: {ok}/{total} titles yielded ≥1 score")
        print("(capture a hit + a soft-miss markdown as Phase 4 fixtures)")
        if ok == 0:
            raise SystemExit("no scores extracted — surface now (Phase 0)")
    else:
        print("done. Eyeball spikes/_out/rt_*.md for Tomatometer/Popcornmeter, then set OPENAI_*.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", nargs="?", default="search", choices=["search", "scrape"])
    args = p.parse_args()

    (api_key,) = _env.require("FIRECRAWL_API_KEY")
    OUT.mkdir(parents=True, exist_ok=True)
    llm_on = bool(_env.get("OPENAI_BASE_URL") and _env.get("OPENAI_EXTRACTION_MODEL"))
    print(f"mode: {args.mode}   |   LLM extraction: {'ON' if llm_on else 'OFF'}   |   maxAge: 1 week\n")

    if args.mode == "search":
        cmd_search(api_key, llm_on)
    else:
        cmd_scrape(api_key, llm_on)


if __name__ == "__main__":
    main()

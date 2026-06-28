"""Spike 05 — Firecrawl scrape -> LLM extracts RT Tomatometer + Popcornmeter.

The RT lane's primary provider is Firecrawl (ADR 0003). This spike proves the two-step
shape we'll build in Phase 4: scrape a rottentomatoes.com page to markdown, then run
`with_structured_output` over that markdown to pull {rt_critic, rt_audience}. Saves the
raw markdown to spikes/_out/ so you can eyeball whether the scores survive the scrape.

Needs FIRECRAWL_API_KEY. LLM extraction additionally needs OPENAI_BASE_URL/KEY +
OPENAI_EXTRACTION_MODEL; without those it just scrapes + saves markdown for eyeballing.

Run:  uv run python spikes/05_firecrawl_rt_extraction.py
"""

from __future__ import annotations

from pathlib import Path

import httpx

import _env

OUT = Path(__file__).resolve().parent / "_out"

# A few known titles (movies + one TV) with well-published RT scores.
RT_URLS = [
    "https://www.rottentomatoes.com/m/dune_part_two",
    "https://www.rottentomatoes.com/m/the_godfather",
    "https://www.rottentomatoes.com/m/parasite_2019",
    "https://www.rottentomatoes.com/tv/the_last_of_us",
]


def scrape_markdown(api_key: str, url: str) -> str:
    """Scrape one URL to markdown. Tries Firecrawl v2, falls back to v1."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    base = _env.get("FIRECRAWL_API_URL")  # optional override
    bases = [base] if base else ["https://api.firecrawl.dev/v2", "https://api.firecrawl.dev/v1"]
    last_err = None
    for b in bases:
        try:
            resp = httpx.post(f"{b}/scrape", headers=headers, json=body, timeout=90)
            if resp.status_code == 404:
                last_err = f"404 at {b}"
                continue
            resp.raise_for_status()
            data = resp.json().get("data", {})
            md = data.get("markdown", "")
            if md:
                return md
            last_err = f"no markdown in response from {b}"
        except httpx.HTTPError as e:
            last_err = f"{type(e).__name__} at {b}: {e}"
    raise RuntimeError(f"scrape failed for {url}: {last_err}")


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


def main() -> None:
    (api_key,) = _env.require("FIRECRAWL_API_KEY")
    OUT.mkdir(parents=True, exist_ok=True)
    llm_on = bool(_env.get("OPENAI_BASE_URL") and _env.get("OPENAI_EXTRACTION_MODEL"))
    print(f"LLM extraction: {'ON' if llm_on else 'OFF (scrape + save only)'}\n")

    ok = 0
    for url in RT_URLS:
        slug = url.rstrip("/").split("/")[-1]
        print(f"• {url}")
        try:
            md = scrape_markdown(api_key, url)
        except Exception as e:  # noqa: BLE001
            print(f"    scrape FAILED: {e}\n")
            continue
        path = OUT / f"rt_{slug}.md"
        path.write_text(md)
        print(f"    scraped {len(md):,} chars -> {path.relative_to(OUT.parent.parent)}")
        scores = extract_scores(md)
        if scores is not None:
            print(f"    extracted: critic={scores.rt_critic}  audience={scores.rt_audience}")
            if scores.rt_critic is not None or scores.rt_audience is not None:
                ok += 1
        print()

    if llm_on:
        print(f"firecrawl+extraction spike: {ok}/{len(RT_URLS)} titles yielded ≥1 score")
        print("(capture a hit + a soft-miss markdown as Phase 4 fixtures)")
        if ok == 0:
            raise SystemExit("no scores extracted — surface now (Phase 0)")
    else:
        print("scrape done. Eyeball spikes/_out/rt_*.md for Tomatometer/Popcornmeter, then")
        print("set OPENAI_* to confirm the LLM can extract them.")


if __name__ == "__main__":
    main()

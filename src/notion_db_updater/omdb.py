"""Async OMDb client — the metadata lane (IMDb rating, IMDb ID, plot, genre).

Resolution is **candidate-shaped from the start** (ADR 0006): `search()` returns the full
`?s=` candidate list rather than a single `?t=` best-match, so the Phase 6a disambiguation
path is *additive* (it consumes the same list) rather than a rewrite. Phase 2 itself only
acts on the 0- and 1-candidate cases (see `graph.py`).

OMDb quirks handled here:
- A miss returns ``{"Response": "False", "Error": "Movie not found!"}`` (HTTP 200), not a
  4xx — `search()` maps that to an empty list (a *definitive* not-found, per ADR 0004).
- Numeric-ish fields arrive as strings and can be the literal ``"N/A"`` — parsed to ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .config import Settings

_API = "https://www.omdbapi.com/"


def _omdb_type(media_type: str | None) -> str | None:
    """Map the Notion `Type` select to OMDb's `type` param, or None to search unfiltered."""
    return {"Movie": "movie", "TV Show": "series"}.get(media_type or "")


# Trailing season / qualifier that OMDb's title search chokes on — a watchlist row is often
# written per-season ("Beef Season 2", "The Bear (S04)") but OMDb indexes the *series*.
_SEASON_SUFFIX = re.compile(
    r"\s*[\(\[]?\s*(?:season\s*\d+|s\d{1,2})\s*[\)\]]?\s*$", re.IGNORECASE
)

# A trailing release year the user appended ("Justice League 2017", "Dune (2021)"). OMDb's
# `?s=` search can't parse a year out of the title string, so it returns 0 — a common `/add`
# miss. Stripped as a fallback; the year still lives in the Entry title, so the disambiguation
# pre-filter keeps it to pick the right year among the (now multi-year) candidates.
_TRAILING_YEAR = re.compile(r"\s*[\(\[]?\s*(?:19|20)\d{2}\s*[\)\]]?\s*$")


def normalize_title(title: str) -> list[str]:
    """Ordered fallback query variants for a title OMDb `?s=` couldn't match (Phase 6f).

    Conservative, *mechanical* rewrites only — the ones the backfill / live use showed OMDb
    search trips over: a trailing season qualifier ("Beef Season 2" → "Beef"), a trailing
    release year ("Justice League 2017" → "Justice League"), `and`↔`&` ("… and …" ↔ "… & …"),
    and stray punctuation ("The Man from U.N.C.L.E" → "The Man from U N C L E"). Returned in
    try-order; the caller searches each until one returns candidates.

    Deliberately *not* a spell/abbreviation fixer: misspellings ("The Oddessey"), abbreviations
    ("Dept. Q"), and roman numerals ("Ne Zha II") can't be repaired mechanically and fall
    through to the human picker (the 6f escalation). Excludes the (whitespace-normalized)
    original and any duplicates.
    """
    original = re.sub(r"\s+", " ", title).strip()
    seen = {original.lower()}
    variants: list[str] = []

    def add(candidate: str) -> None:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        key = candidate.lower()
        if candidate and key not in seen:
            seen.add(key)
            variants.append(candidate)

    stripped = _SEASON_SUFFIX.sub("", original)
    add(stripped)
    year_stripped = _TRAILING_YEAR.sub("", original)
    add(year_stripped)
    for base in (original, stripped, year_stripped):
        add(re.sub(r"\s+and\s+", " & ", base, flags=re.IGNORECASE))
        add(re.sub(r"\s*&\s*", " and ", base))
        add(re.sub(r"[.\-:_/]+", " ", base))
    return variants


def _clean(value: str | None) -> str | None:
    """OMDb string field → its value, or None when missing / the literal "N/A"."""
    if value is None:
        return None
    value = value.strip()
    return value or None if value != "N/A" else None


def parse_rating(value: str | None) -> float | None:
    """Parse an OMDb `imdbRating` ("8.1" / "N/A" / None) into a float or None."""
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_year(value: str | None) -> int | None:
    """Parse an OMDb `Year` into an int, taking the first 4-digit run.

    OMDb series carry a range ("2013–2017", "2013–") and movies a plain year ("2021"); we
    keep the *start* year — enough for the Judge's cross-lane title/year correlation (Phase 5).
    """
    text = _clean(value)
    if text is None:
        return None
    match = re.search(r"\d{4}", text)
    return int(match.group()) if match else None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One OMDb `?s=` search result — a possible match for a typed title.

    The unit of disambiguation (ADR 0006). Phase 2 uses only the single-candidate case;
    Phase 6a surfaces the whole list to the LLM pre-filter / Slack picker.
    """

    imdb_id: str
    title: str | None
    year: str | None
    media_type: str | None  # OMDb's "movie" | "series" | "episode"
    poster: str | None  # poster URL (or None); used by the Phase 6c HITL picker

    @classmethod
    def from_search(cls, item: dict) -> Candidate:
        return cls(
            imdb_id=item["imdbID"],
            title=_clean(item.get("Title")),
            year=_clean(item.get("Year")),
            media_type=_clean(item.get("Type")),
            poster=_clean(item.get("Poster")),
        )


class OMDbClient:
    """Thin async wrapper over the OMDb REST API.

    Use as an async context manager so the HTTP connection pool is closed::

        async with OMDbClient(settings) as omdb:
            candidates = await omdb.search("Dune", "Movie")
    """

    def __init__(self, settings: Settings) -> None:
        self._apikey = settings.OMDB_API_KEY
        self._client = httpx.AsyncClient(base_url=_API, timeout=30.0)

    async def __aenter__(self) -> OMDbClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, params: dict) -> dict:
        resp = await self._client.get("", params={"apikey": self._apikey, **params})
        resp.raise_for_status()
        return resp.json()

    async def raw(self, **params: str) -> dict:
        """Raw OMDb response for arbitrary params — for fixture capture / debugging only.

        Prefer `search()` / `details()` in real code; this exists so the CLI can snapshot
        unparsed `?s=` / not-found responses into `tests/fixtures/`.
        """
        return await self._get(params)

    async def search(self, title: str, media_type: str | None = None) -> list[Candidate]:
        """Search OMDb (`?s=`) for a title; return the candidate list.

        `media_type` is the Notion `Type` ("Movie" | "TV Show" | None); when set it narrows
        the search via OMDb's `type` param. A definitive not-found returns ``[]``.
        """
        params: dict = {"s": title}
        omdb_type = _omdb_type(media_type)
        if omdb_type:
            params["type"] = omdb_type
        data = await self._get(params)
        if data.get("Response") != "True":
            return []  # {"Response": "False", "Error": "Movie not found!"} → definitive miss
        return [Candidate.from_search(item) for item in data.get("Search", [])]

    async def details(self, imdb_id: str) -> dict:
        """Fetch full details for one IMDb id (`?i=`, `plot=full`). Returns the raw dict."""
        return await self._get({"i": imdb_id, "plot": "full"})


def details_fields(details: dict) -> dict:
    """Extract the enrichment + identity fields from a `?i=` details response.

    Keeps OMDb's field names (imdbID / imdbRating / Plot / Genre / Year / Type) confined to
    this module; the graph node consumes the normalized dict. Alongside the Phase-2 metadata,
    Phase 5 pulls the **resolved identity** — `omdb_title`, `year`, `omdb_type` — so the Judge
    can correlate against the RT lane's independently-resolved page (ADR 0008).
    """
    return {
        "imdb_id": _clean(details.get("imdbID")),
        "imdb_rating": parse_rating(details.get("imdbRating")),
        "plot": _clean(details.get("Plot")),
        "genre": _clean(details.get("Genre")),
        "omdb_title": _clean(details.get("Title")),
        "year": parse_year(details.get("Year")),
        "omdb_type": _clean(details.get("Type")),
    }

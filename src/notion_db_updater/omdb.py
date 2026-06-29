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

from dataclasses import dataclass

import httpx

from .config import Settings

_API = "https://www.omdbapi.com/"


def _omdb_type(media_type: str | None) -> str | None:
    """Map the Notion `Type` select to OMDb's `type` param, or None to search unfiltered."""
    return {"Movie": "movie", "TV Show": "series"}.get(media_type or "")


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


@dataclass(frozen=True, slots=True)
class Candidate:
    """One OMDb `?s=` search result — a possible match for a typed Title.

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
        """Search OMDb (`?s=`) for a typed Title; return the candidate list.

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
    """Extract the Phase 2 enrichment fields from a `?i=` details response.

    Keeps OMDb's field names (imdbID / imdbRating / Plot / Genre) confined to this module;
    the graph node consumes the normalized dict. RT scores and the year/type live in later
    phases — only the metadata-lane fields are pulled here.
    """
    return {
        "imdb_id": _clean(details.get("imdbID")),
        "imdb_rating": parse_rating(details.get("imdbRating")),
        "plot": _clean(details.get("Plot")),
        "genre": _clean(details.get("Genre")),
    }

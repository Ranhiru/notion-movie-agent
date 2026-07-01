"""The `EnrichedEntry` output contract — Phase 5 (RESEARCH §5, ADR 0008).

`EnrichedEntry` is the graph's typed result: the assembled OMDb + RT record plus the Judge's
`confidence`. It is built once, at the `judge` node (the only point where every field is final
— the RT scores may be rewritten by `resolve_rt`, and `confidence` is the Judge's own output).

`confidence` lives here (and in the LangSmith trace / graph state) only — it is deliberately
*not* a §8 Notion property (ADR 0008), so `update_notion` never writes it.

`media_type` is normalized to the two-value `{movie, tv}` domain of the contract; the raw
inputs are OMDb's `movie/series/episode` and the Notion `Type` select's `Movie/TV Show`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "tv"]
Confidence = Literal["high", "medium", "low"]

# Raw media-type strings → the contract's two-value domain. OMDb: movie/series/episode;
# Notion `Type` select: Movie/TV Show. Anything unknown falls back to "movie".
_MEDIA_TYPE = {
    "movie": "movie",
    "series": "tv",
    "episode": "tv",
    "Movie": "movie",
    "TV Show": "tv",
}


def normalize_media_type(value: str | None) -> MediaType:
    """Map an OMDb or Notion media-type string to the contract's `{movie, tv}` domain."""
    return _MEDIA_TYPE.get(value or "", "movie")  # type: ignore[return-value]


class EnrichedEntry(BaseModel):
    """The enrichment output contract for one Entry (RESEARCH §5).

    Assembled deterministically from graph state; `confidence` is supplied by the Judge. Only
    `title`, `media_type`, and `confidence` are guaranteed — every enriched field is nullable
    because enrichment is best-effort (RT especially; ADR 0003 / 0004).
    """

    title: str
    year: int | None = None
    media_type: MediaType
    imdb_id: str | None = None
    imdb_rating: float | None = None  # 0–10
    rt_critic: int | None = None  # 0–100, Tomatometer
    rt_audience: int | None = None  # 0–100, Popcornmeter
    plot: str | None = None
    genre: str | None = None  # comma-separated, from OMDb
    confidence: Confidence  # set by the Judge (trace-only; never written to Notion)
    sources_used: list[str] = Field(default_factory=list)

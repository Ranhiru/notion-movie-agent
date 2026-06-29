"""Domain models for reading the Watchlist.

`Entry` is the parsed, agent-facing view of one Notion page (one Watchlist entry — a movie
or TV show), mapping the §8 properties off the raw Notion JSON. Its `.title` holds the value
of the Notion "Title" property. The entity is an `Entry`, so its variables are named `entry`
— never `title`, which would collide with `.title` (the original `title.title` problem).
Field names follow the ubiquitous language in CONTEXT.md ("Entry", "Enrichment Status", …)
and the §8 "Maps to" column.
"""

from __future__ import annotations

from dataclasses import dataclass

# §8 property names — the contract against the live Watchlist (verified by spike 01).
PROP_TITLE = "Title"  # title
PROP_TYPE = "Type"  # select [Movie, TV Show]
PROP_IMDB_RATING = "IMDB Rating"  # number
PROP_RT_CRITIC = "RT Critic Score"  # number
PROP_RT_AUDIENCE = "RT Audience Score"  # number
PROP_PLOT = "Plot Summary"  # rich_text
PROP_GENRE = "Genre"  # rich_text
PROP_STATUS = "Enrichment Status"  # select [pending, awaiting_input, done, failed]

# Every §8 property the agent reads — used for the drift check on a real row.
EXPECTED_PROPERTIES = (
    PROP_TITLE,
    PROP_TYPE,
    PROP_IMDB_RATING,
    PROP_RT_CRITIC,
    PROP_RT_AUDIENCE,
    PROP_PLOT,
    PROP_GENRE,
    PROP_STATUS,
)


def _title_text(props: dict) -> str | None:
    """Plain text of a Notion `title` property, or None if blank."""
    parts = props.get(PROP_TITLE, {}).get("title", [])
    text = "".join(p.get("plain_text", "") for p in parts).strip()
    return text or None


def _select_name(props: dict, prop: str) -> str | None:
    """Selected option name of a Notion `select` property, or None if unset."""
    sel = props.get(prop, {}).get("select")
    return sel.get("name") if sel else None


def _number(props: dict, prop: str) -> float | None:
    """Value of a Notion `number` property, or None if unset."""
    return props.get(prop, {}).get("number")


def _rich_text(props: dict, prop: str) -> str | None:
    """Concatenated plain text of a Notion `rich_text` property, or None if blank."""
    parts = props.get(prop, {}).get("rich_text", [])
    text = "".join(p.get("plain_text", "") for p in parts).strip()
    return text or None


def enrichment_properties(
    *,
    imdb_rating: float | None = None,
    plot: str | None = None,
    genre: str | None = None,
    status: str,
) -> dict:
    """Serialize enrichment results into a Notion `properties` payload (the write-back of §8).

    Only the fields that were actually found are included (partial-data writes per ADR 0004);
    `Enrichment Status` is always set. Shapes proven live by `spikes/01_notion_data_source.py`:
    number, rich_text, select.
    """
    props: dict = {PROP_STATUS: {"select": {"name": status}}}
    if imdb_rating is not None:
        props[PROP_IMDB_RATING] = {"number": imdb_rating}
    if plot is not None:
        props[PROP_PLOT] = {"rich_text": [{"text": {"content": plot}}]}
    if genre is not None:
        props[PROP_GENRE] = {"rich_text": [{"text": {"content": genre}}]}
    return props


@dataclass(frozen=True, slots=True)
class Entry:
    """One Watchlist entry (a movie or TV show), parsed from a Notion page (§8 projection)."""

    page_id: str
    title: str | None  # the entry's title; None for a blank row (matches the is_empty filter)
    media_type: str | None  # "Movie" | "TV Show" | None
    imdb_rating: float | None
    rt_critic: float | None
    rt_audience: float | None
    plot: str | None
    genre: str | None
    status: str | None  # pending | awaiting_input | done | failed | None (unset)

    @classmethod
    def from_page(cls, page: dict) -> Entry:
        """Parse a raw Notion page object (query result or page fetch) into an Entry."""
        props = page.get("properties", {})
        return cls(
            page_id=page["id"],
            title=_title_text(props),
            media_type=_select_name(props, PROP_TYPE),
            imdb_rating=_number(props, PROP_IMDB_RATING),
            rt_critic=_number(props, PROP_RT_CRITIC),
            rt_audience=_number(props, PROP_RT_AUDIENCE),
            plot=_rich_text(props, PROP_PLOT),
            genre=_rich_text(props, PROP_GENRE),
            status=_select_name(props, PROP_STATUS),
        )

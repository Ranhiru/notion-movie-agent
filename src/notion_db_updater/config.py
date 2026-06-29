"""Typed application settings, loaded from the project-root `.env` (dev) or the shell.

Replaces the throwaway `spikes/_env.py` glue with a single validated `Settings` object.
Every key is from RESEARCH §10. Phase 1 only *needs* `NOTION_MOVIE_DB_TOKEN`; everything
else is optional/defaulted so the app boots before later phases' keys are filled in. The
LangSmith block is pure config — present + `LANGSMITH_TRACING=true` is what "tracing ON"
means; there is no graph to trace yet in Phase 1.

Usage:
    from notion_db_updater.config import get_settings
    settings = get_settings()   # raises a clear error if a required key is missing
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = two levels up from this file (src/notion_db_updater/config.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"

# Notion API version pinned per project conventions (databases split into data sources).
NOTION_VERSION = "2025-09-03"
# Watchlist data source id — pre-filled, not a secret (RESEARCH §8).
DEFAULT_WATCHLIST_DATA_SOURCE_ID = "ffcdcd68-0449-461d-be8e-0af9b71f9d5f"


class Settings(BaseSettings):
    """All runtime configuration, validated from env. Shell-exported vars win over `.env`."""

    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unrelated env vars in the shell
        case_sensitive=True,
    )

    # --- Notion (data model §8) — the only key Phase 1 requires ---
    NOTION_MOVIE_DB_TOKEN: str
    NOTION_WATCHLIST_DATA_SOURCE_ID: str = DEFAULT_WATCHLIST_DATA_SOURCE_ID

    # --- OMDb (metadata lane — Phase 2) ---
    OMDB_API_KEY: str = ""

    # --- RT resolution providers (chain — Firecrawl primary in Phase 4, rest Phase 8) ---
    FIRECRAWL_API_KEY: str = ""
    TAVILY_API_KEY: str = ""
    EXA_API_KEY: str = ""
    PERPLEXITY_API_KEY: str = ""
    SEARCH_PROVIDERS: str = "firecrawl,tavily,exa,perplexity"

    # --- LLM endpoint (OpenAI-compatible — ADR 0011; used from Phase 4) ---
    OPENAI_BASE_URL: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_EXTRACTION_MODEL: str = ""
    OPENAI_DISAMBIGUATION_MODEL: str = ""
    OPENAI_JUDGE_MODEL: str = ""

    # --- Slack HITL transport (Socket Mode — ADR 0010; used from Phase 6) ---
    SLACK_BOT_TOKEN: str = ""
    SLACK_APP_TOKEN: str = ""
    SLACK_CHANNEL: str = "#notion-movie-db"

    # --- LangSmith tracing (ON from Phase 1; pure config) ---
    LANGSMITH_TRACING: bool = True
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_ENDPOINT: str = "https://apac.api.smith.langchain.com"
    LANGSMITH_PROJECT: str = "NotionMovieDBAgent"

    @property
    def search_providers(self) -> list[str]:
        """`SEARCH_PROVIDERS` parsed into an ordered list of provider names."""
        return [p.strip() for p in self.SEARCH_PROVIDERS.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    """Build (once) and return the validated Settings.

    Raises ``pydantic.ValidationError`` with a clear message if a required key
    (currently just ``NOTION_MOVIE_DB_TOKEN``) is unset.
    """
    return Settings()  # type: ignore[call-arg]  # values come from env, not args

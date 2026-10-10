"""Opt-in model evaluation against fixture Watchlist and research services.

Run with `python -m notion_db_updater.evaluation`. The configured agent model is real, but
all Watchlist operations use an in-memory fixture Runtime and a temporary conversation DB.
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from .agent import build_planner
from .config import Settings, get_settings
from .conversation import Conversation
from .llm import agent_model
from .slack import SlackTransport

_IDENTITIES = {
    "tt1091191": ("Lone Survivor", 2013),
    "tt0265086": ("Black Hawk Down", 2001),
    "tt0133093": ("The Matrix", 1999),
    "tt0068646": ("The Godfather", 1972),
    "tt0088763": ("Back to the Future", 1985),
    "tt0111161": ("The Shawshank Redemption", 1994),
    "tt0109830": ("Forrest Gump", 1994),
    "tt1375666": ("Inception", 2010),
}


class FixtureResearchClient:
    """Fixture-backed public content for model evaluation; never opens a network connection."""

    async def search(self, query: str) -> list[dict[str, str]]:
        return [
            {
                "url": "https://public.example/evidence",
                "title": "Fixture source",
                "content": f"Fixture evidence for: {query}",
            }
        ]

    async def fetch_page(self, url: str) -> str:
        if url != "https://public.example/list":
            raise ValueError("FixtureResearchClient only serves its public example page")
        return "Popular films: The Matrix (1999); The Godfather (1972)."


class FixtureRuntime:
    """Small deterministic Runtime fake; no Notion, enrichment, or external writes."""

    def __init__(self, research: FixtureResearchClient) -> None:
        self.research = research
        self.entries: dict[str, SimpleNamespace] = {}
        self.writes: list[tuple[str, str]] = []

    async def list_watchlist(self, unwatched: bool = False) -> list[SimpleNamespace]:
        entries = [entry for entry in self.entries.values() if not entry.removed]
        if unwatched:
            entries = [entry for entry in entries if entry.watched is False]
        return entries

    async def validate_identity(self, imdb_id: str) -> dict[str, Any]:
        if imdb_id not in _IDENTITIES:
            raise ValueError(f"Unknown fixture IMDb ID: {imdb_id}")
        title, year = _IDENTITIES[imdb_id]
        return {"imdb_id": imdb_id, "title": title, "year": year, "media_type": "Movie"}

    async def resolve_title(
        self, query: str, media_type: str | None = None
    ) -> list[dict[str, Any]]:
        folded = query.casefold()
        return [
            await self.validate_identity(imdb_id)
            for imdb_id, (title, _year) in _IDENTITIES.items()
            if title.casefold() in folded or folded in title.casefold()
        ]

    async def add_resolved(
        self,
        imdb_id: str,
        original_request: str,
        channel: str,
        user: str,
        thread_ts: str,
        request_key: str,
    ) -> dict[str, str]:
        for entry in self.entries.values():
            if entry.imdb_id == imdb_id and not entry.removed:
                return {
                    "status": "already_present",
                    "page_id": entry.page_id,
                    "title": entry.title,
                }
        title, year = _IDENTITIES[imdb_id]
        page_id = f"fixture-{imdb_id}"
        self.entries[page_id] = SimpleNamespace(
            page_id=page_id,
            imdb_id=imdb_id,
            title=title,
            year=year,
            watched=False,
            removed=False,
            genre="Drama",
            plot=f"A fixture synopsis of {title}.",
            imdb_rating=8.0,
            rt_critic=80,
            rt_audience=85,
            media_type="Movie",
        )
        self.writes.append(("add", imdb_id))
        return {"status": "done", "page_id": page_id, "title": title}

    async def mutate_entry(
        self, page_id: str, operation: str, **_kwargs: Any
    ) -> dict[str, str]:
        entry = self.entries[page_id]
        if operation == "remove":
            entry.removed = True
        elif operation in {"watched", "unwatched"}:
            entry.watched = operation == "watched"
        self.writes.append((operation, page_id))
        return {"status": operation, "page_id": page_id, "title": entry.title}

    async def search(self, query: str) -> list[dict[str, str]]:
        return await self.research.search(query)

    async def fetch_page(self, url: str) -> str:
        return await self.research.fetch_page(url)


@dataclass
class _Harness:
    runtime: FixtureRuntime
    transport: SlackTransport
    db_path: str
    settings: Settings

    def restart(self) -> None:
        conversation = Conversation(
            self.runtime,
            self.db_path,
            build_planner(agent_model(self.settings)),
            research=self.runtime.research,
        )
        self.transport._runtime = cast(Any, SimpleNamespace(conversation=conversation))


def _harness(settings: Settings, db_path: str) -> _Harness:
    from types import SimpleNamespace as Namespace

    runtime = FixtureRuntime(FixtureResearchClient())
    conversation = Conversation(
        runtime,
        db_path,
        build_planner(agent_model(settings)),
        research=runtime.research,
    )
    transport = object.__new__(SlackTransport)
    transport._runtime = cast(Any, Namespace(conversation=conversation))
    transport._tasks = set()
    return _Harness(runtime, transport, db_path, settings)


async def _send(
    harness: _Harness, text: str, *, thread: str, ts: str, mention: bool = False
) -> str:
    from unittest.mock import AsyncMock

    say = AsyncMock()
    event_text = f"<@BOT> {text}" if mention else text
    event = {
        "text": event_text,
        "channel": "C-FIXTURE",
        "user": "fixture-user",
        "ts": ts,
        "team": "T-FIXTURE",
    }
    if not mention:
        event["thread_ts"] = thread
    await harness.transport.handle_conversation(event, say, "T-FIXTURE")
    if say.await_args is None:
        raise AssertionError("Slack transport produced no threaded reply")
    return say.await_args.kwargs["text"]


def _safe_error(exc: Exception, settings: Settings | None = None) -> str:
    message = str(exc)
    if settings is not None:
        secrets = (
            settings.OPENAI_API_KEY,
            settings.NOTION_MOVIE_DB_TOKEN,
            settings.SLACK_BOT_TOKEN,
            settings.SLACK_APP_TOKEN,
            settings.TAVILY_API_KEY,
        )
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
    return f"{type(exc).__name__}: {message}"


async def _run_scenario(
    name: str,
    scenario,
    settings: Settings,  # noqa: ANN001
) -> tuple[str, bool, str]:
    try:
        detail = await scenario
        return name, True, detail
    except Exception as exc:  # evaluation should report every failed behavior
        return name, False, _safe_error(exc, settings)


async def evaluate(settings: Settings) -> list[tuple[str, bool, str]]:
    """Run model behaviors against fixture services and isolated durable state."""
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="watchlist-evaluation-") as temporary:
        harness = _harness(settings, str(Path(temporary) / "conversation.sqlite"))

        async def multi_add() -> str:
            await _send(
                harness,
                "Add Lone Survivor and Black Hawk Down.",
                thread="multi",
                ts="1",
                mention=True,
            )
            ids = {entry.imdb_id for entry in harness.runtime.entries.values()}
            expected = {"tt1091191", "tt0265086"}
            if ids != expected:
                raise AssertionError(f"Expected only requested identities, got {sorted(ids)}")
            return "only the two requested IMDb identities were added"

        results.append(await _run_scenario("multi-title add", multi_add(), settings))

        async def read_only_intent() -> str:
            before = len(harness.runtime.writes)
            for index, text in enumerate(
                (
                    "Do not add The Matrix. Just tell me whether it is on my Watchlist.",
                    "Should I add The Godfather? What do you think?",
                    "Recommend something from my existing unwatched Watchlist.",
                )
            ):
                await _send(harness, text, thread=f"intent-{index}", ts="1", mention=True)
                if len(harness.runtime.writes) != before:
                    raise AssertionError(f"Read-only request changed the Watchlist: {text}")
            return "negation, advice, and recommendations caused no Watchlist changes"

        results.append(await _run_scenario("read-only intent", read_only_intent(), settings))

        async def url_import() -> str:
            writes_before = len(harness.runtime.writes)
            response = await _send(
                harness,
                "Import titles from https://public.example/list",
                thread="2",
                ts="2",
                mention=True,
            )
            if "approve " not in response:
                raise AssertionError("URL Import did not produce an approval preview")
            if len(harness.runtime.writes) != writes_before:
                raise AssertionError("URL Import wrote before approval")
            if "The Matrix" not in response or "The Godfather" not in response:
                raise AssertionError("The preview omitted one or more page titles")
            token_match = re.search(r"approve `?([0-9a-f]{16})", response)
            if not token_match:
                raise AssertionError("Could not read the preview approval token")
            selected = await _send(
                harness,
                f"approve {token_match.group(1)} 1,2",
                thread="2",
                ts="3",
            )
            if "The Matrix" not in selected or "The Godfather" not in selected:
                raise AssertionError(f"Approved import result was incomplete: {selected}")
            return "two extracted titles waited for approval, then were added"

        results.append(await _run_scenario("URL preview and approval", url_import(), settings))

        async def web_search_scenario() -> str:
            response = await _send(
                harness,
                "Use web search to find a source about The Matrix and cite the source.",
                thread="search",
                ts="4",
                mention=True,
            )
            if "public.example/evidence" not in response:
                raise AssertionError("The answer did not cite the fixture search result")
            return "the fixture search result was used and cited"

        results.append(
            await _run_scenario("web search and citation", web_search_scenario(), settings)
        )

        async def approval_restart() -> str:
            before = len(harness.runtime.writes)
            response = await _send(
                harness,
                (
                    "Add Back to the Future, The Shawshank Redemption, Forrest Gump, and "
                    "Inception."
                ),
                thread="5",
                ts="5",
                mention=True,
            )
            if "approve " not in response or len(harness.runtime.writes) != before:
                raise AssertionError("Four-entry request wrote before its approval preview")
            token_match = re.search(r"approve `?([0-9a-f]{16})", response)
            if not token_match:
                raise AssertionError("Could not read the bulk approval token")
            harness.restart()
            result = await _send(
                harness, f"approve {token_match.group(1)}", thread="5", ts="6"
            )
            added = {entry.imdb_id for entry in harness.runtime.entries.values()}
            expected = set(_IDENTITIES)
            if not expected <= added:
                raise AssertionError(
                    f"Restarted approval missed identities: {sorted(expected - added)}"
                )
            if "Back to the Future" not in result:
                raise AssertionError(f"Approval result did not summarize adds: {result}")
            return "four-entry preview survived restart and wrote only after approval"

        results.append(
            await _run_scenario("bulk approval and restart", approval_restart(), settings)
        )

        async def recommendation_followup() -> str:
            response = await _send(
                harness,
                "Recommend something unwatched.",
                thread="7",
                ts="7",
                mention=True,
            )
            if not any(entry.title in response for entry in harness.runtime.entries.values()):
                raise AssertionError("Recommendations did not use the fixture Watchlist")
            harness.restart()
            changed = await _send(
                harness,
                "Mark the first recommendation watched.",
                thread="7",
                ts="8",
            )
            if not any(operation == "watched" for operation, _ in harness.runtime.writes):
                raise AssertionError(f"Numbered follow-up did not change Watched: {changed}")
            return (
                "recommendations used stored unwatched Entries and the numbered follow-up "
                "survived restart"
            )

        results.append(
            await _run_scenario(
                "unwatched recommendation follow-up", recommendation_followup(), settings
            )
        )
    return results


def main() -> int:
    settings: Settings | None = None
    try:
        settings = get_settings()
        if not settings.OPENAI_AGENT_MODEL:
            raise ValueError("OPENAI_AGENT_MODEL must be configured for live evaluation")
        results = asyncio.run(evaluate(settings))
    except Exception as exc:
        print(f"Evaluation could not start: {_safe_error(exc, settings)}", file=sys.stderr)
        return 2

    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

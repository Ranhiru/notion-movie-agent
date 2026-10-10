from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver

from notion_db_updater.app import Runtime
from notion_db_updater.checkpoint import checkpoint_serde
from notion_db_updater.config import Settings
from notion_db_updater.conversation import Action, Conversation, Reply
from notion_db_updater.graph import CompletionNotifier, JudgeVerdict, build_graph
from notion_db_updater.models import (
    PROP_IMDB_ID,
    PROP_STATUS,
    PROP_TITLE,
    PROP_TYPE,
    PROP_WATCHED,
)
from notion_db_updater.notion import NotionClient
from notion_db_updater.omdb import Candidate
from notion_db_updater.slack import SlackTransport


def _text_property(kind: str, value: str | None = None) -> dict:
    parts = [] if value is None else [{"plain_text": value, "text": {"content": value}}]
    return {"type": kind, kind: parts}


def _properties(title: str, imdb_id: str | None = None, media_type: str = "Movie") -> dict:
    props = {
        PROP_TITLE: _text_property("title", title),
        PROP_TYPE: {"type": "select", "select": {"name": media_type}},
        PROP_WATCHED: {"type": "checkbox", "checkbox": False},
        PROP_STATUS: {"type": "select", "select": {"name": "pending"}},
    }
    if imdb_id is not None:
        props[PROP_IMDB_ID] = _text_property("rich_text", imdb_id)
    return props


class _NotionStore:
    """Small Notion REST model behind MockTransport, including create-then-timeout."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.create_count = 0
        self.timeout_after_create = False
        self.schema = {
            "properties": {
                PROP_TITLE: {"type": "title"},
                PROP_TYPE: {"type": "select"},
                PROP_WATCHED: {"type": "checkbox"},
                PROP_IMDB_ID: {"type": "rich_text"},
                PROP_STATUS: {"type": "select"},
            }
        }

    async def request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/data_sources/watchlist":
            return httpx.Response(200, json=self.schema)
        if request.method == "POST" and path == "/v1/data_sources/watchlist/query":
            body = json.loads(request.content)
            rows = [p for p in self.pages.values() if not p.get("archived")]
            filter_ = body.get("filter")
            if filter_:
                rows = [p for p in rows if self._matches(p, filter_)]
            return httpx.Response(
                200,
                json={"results": rows[:100], "has_more": False, "next_cursor": None},
            )
        if request.method == "POST" and path == "/v1/pages":
            body = json.loads(request.content)
            self.create_count += 1
            page_id = f"page-{self.create_count}"
            page = {
                "id": page_id,
                "properties": self._normalize_updates(body["properties"]),
                "archived": False,
                "in_trash": False,
            }
            self.pages[page_id] = page
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise httpx.ReadTimeout("response lost after remote create", request=request)
            return httpx.Response(200, json=page)
        if path.startswith("/v1/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            page = self.pages.get(page_id)
            if page is None:
                return httpx.Response(404, json={"message": "not found"})
            if request.method == "GET":
                return httpx.Response(200, json=page)
            if request.method == "PATCH":
                body = json.loads(request.content)
                if "properties" in body:
                    page["properties"].update(self._normalize_updates(body["properties"]))
                if "archived" in body:
                    page["archived"] = body["archived"]
                return httpx.Response(200, json=page)
        return httpx.Response(404, json={"message": "unsupported fake Notion endpoint"})

    @staticmethod
    def _normalize_updates(updates: dict) -> dict:
        normalized = {}
        for name, value in updates.items():
            kind, raw = next(iter(value.items()))
            if kind in {"title", "rich_text"}:
                parts = raw or []
                normalized[name] = {
                    "type": kind,
                    kind: [
                        {
                            "plain_text": part.get("plain_text")
                            or part.get("text", {}).get("content", ""),
                            "text": part.get("text", {}),
                        }
                        for part in parts
                    ],
                }
            else:
                normalized[name] = {"type": kind, kind: raw}
        return normalized

    @staticmethod
    def _matches(page: dict, filter_: dict) -> bool:
        if "or" in filter_:
            return any(_NotionStore._matches(page, child) for child in filter_["or"])
        name = filter_["property"]
        prop = page["properties"].get(name, {})
        if "title" in filter_:
            expected = filter_["title"].get("contains", "").casefold()
            actual = "".join(p.get("plain_text", "") for p in prop.get("title", []))
            return expected in actual.casefold()
        if "rich_text" in filter_:
            expected = filter_["rich_text"].get("equals")
            actual = "".join(p.get("plain_text", "") for p in prop.get("rich_text", []))
            return actual == expected
        if "checkbox" in filter_:
            return prop.get("checkbox") is filter_["checkbox"].get("equals")
        if "select" in filter_:
            value = prop.get("select")
            if "is_empty" in filter_["select"]:
                return (not value) is filter_["select"]["is_empty"]
            return bool(value and value.get("name") == filter_["select"].get("equals"))
        return False


class _FakeOmDb:
    def __init__(self, definitions: dict[str, dict], aliases: dict[str, list[str]]) -> None:
        self.definitions = definitions
        self.aliases = {key.casefold(): value for key, value in aliases.items()}
        self.searches: list[str] = []

    async def search(self, title: str, media_type: str | None = None) -> list[Candidate]:
        self.searches.append(title)
        return [self._candidate(imdb_id) for imdb_id in self.aliases.get(title.casefold(), [])]

    async def details(self, imdb_id: str) -> dict:
        return {"Response": "True", **self.definitions[imdb_id]}

    def _candidate(self, imdb_id: str) -> Candidate:
        details = self.definitions[imdb_id]
        return Candidate(
            imdb_id=imdb_id,
            title=details["Title"],
            year=details["Year"],
            media_type=details["Type"],
            poster=None,
        )


class _FakeSearch:
    async def search_rt_candidates(self, title: str, media_type: str | None = None) -> list:
        return []


class _StructuredModel:
    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts

    def with_structured_output(self, schema):
        async def respond(prompt: str):
            self.prompts.append(prompt)
            if schema is JudgeVerdict:
                return JudgeVerdict(confidence="high", wrong_match=False, reason="fixture")
            return schema()

        return RunnableLambda(respond)


class WatchlistIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = _NotionStore()
        settings = Settings(
            NOTION_MOVIE_DB_TOKEN="fixture-token",
            NOTION_WATCHLIST_DATA_SOURCE_ID="watchlist",
            NOTION_RPS=1000,
            RECONCILE_CONCURRENCY=2,
            CHECKPOINT_DB_PATH=str(Path(self.temp.name) / "unused.sqlite"),
            GRAPH_MAX_CONCURRENCY=0,
        )
        self.notion = NotionClient(settings)
        await self.notion._client.aclose()
        self.notion._client = httpx.AsyncClient(
            base_url="https://api.notion.com/v1",
            transport=httpx.MockTransport(self.store.request),
            headers={"Notion-Version": "2025-09-03", "Authorization": "Bearer test"},
        )
        self.definitions = {
            "tt0000001": {
                "imdbID": "tt0000001",
                "Title": "Dune",
                "Year": "2021",
                "Type": "movie",
                "imdbRating": "8.0",
                "Plot": "A desert planet.",
                "Genre": "Adventure, Drama",
            },
            "tt0000002": {
                "imdbID": "tt0000002",
                "Title": "The Thing",
                "Year": "1982",
                "Type": "movie",
                "imdbRating": "8.2",
                "Plot": "An Antarctic station.",
                "Genre": "Horror",
            },
            "tt0000003": {
                "imdbID": "tt0000003",
                "Title": "The Thing",
                "Year": "2011",
                "Type": "movie",
                "imdbRating": "6.2",
                "Plot": "A Norwegian outpost.",
                "Genre": "Horror",
            },
            "tt0000004": {
                "imdbID": "tt0000004",
                "Title": "Arrival",
                "Year": "2016",
                "Type": "movie",
                "imdbRating": "7.9",
                "Plot": "First contact.",
                "Genre": "Drama, Sci-Fi",
            },
        }
        aliases = {
            "dune": ["tt0000001"],
            "arrakis": ["tt0000001"],
            "arrakis: the desert": ["tt0000001"],
            "the thing 1982": ["tt0000002"],
            "the thing 2011": ["tt0000003"],
            "arrival": ["tt0000004"],
        }
        self.omdb = _FakeOmDb(self.definitions, aliases)
        prompts: list[str] = []
        model = _StructuredModel(prompts)
        self.judge_prompts = prompts
        self.runtime = object.__new__(Runtime)
        self.runtime._settings = settings
        self.runtime._notion = self.notion
        self.runtime._omdb = self.omdb
        self.runtime._concurrency = settings.RECONCILE_CONCURRENCY
        self.runtime._lock = asyncio.Lock()
        self.runtime._graph = build_graph(
            self.notion,
            self.omdb,
            _FakeSearch(),
            model,
            model,
            model,
            checkpointer=InMemorySaver(serde=checkpoint_serde()),
        )
        self.runtime._notifier = None
        self.runtime._completion_notifier = CompletionNotifier()
        self.runtime._inflight = set()
        self.runtime._identity_lock = asyncio.Lock()
        self.runtime._page_locks = {}
        self.runtime.conversation = Conversation(
            self.runtime,
            str(Path(self.temp.name) / "conversations.sqlite"),
            "UOWNER",
            self._plan,
        )
        await self.runtime.ensure_watchlist_schema()
        self.transport = object.__new__(SlackTransport)
        self.transport._runtime = self.runtime
        self.transport._tasks = set()
        self.say = AsyncMock()
        self._request_num = 0

    async def asyncTearDown(self) -> None:
        await self.notion.aclose()
        self.temp.cleanup()

    async def _plan(self, messages: list[dict], tools) -> Reply:
        text = messages[-1]["content"].strip()
        lowered = text.casefold()
        if lowered.startswith("add "):
            query = text[4:].strip()
            candidates = await tools.resolve_title(query)
            if candidates:
                tools.stage(Action(operation="add", imdb_id=candidates[0]["imdb_id"]))
            return Reply(text="Staged the resolved title.")
        if lowered == "mark watched":
            entry = (await tools.watchlist())[0]
            tools.stage(Action(operation="watched", page_id=entry["page_id"]))
            return Reply(text="Marking it watched.")
        if lowered == "mark unwatched":
            entry = (await tools.watchlist())[0]
            tools.stage(Action(operation="unwatched", page_id=entry["page_id"]))
            return Reply(text="Marking it unwatched.")
        if lowered == "refresh":
            entry = (await tools.watchlist())[0]
            tools.stage(Action(operation="refresh", page_id=entry["page_id"]))
            return Reply(text="Refreshing it.")
        if lowered == "remove":
            entry = (await tools.watchlist())[0]
            tools.stage(Action(operation="remove", page_id=entry["page_id"]))
            return Reply(text="Removing it.")
        return Reply(text="Please clarify.")

    async def _ask(self, text: str) -> str:
        self._request_num += 1
        timestamp = f"17100000.{self._request_num:03d}"
        await self.transport._handle_mention(
            {
                "text": f"<@UBOT> {text}",
                "channel": "CWATCH",
                "team": "TWORK",
                "user": "UOWNER",
                "ts": timestamp,
            },
            self.say,
            Mock(),
            team_id="TWORK",
        )
        tasks = list(self.transport._tasks)
        if tasks:
            await asyncio.gather(*tasks)
        self.assertTrue(self.say.await_args_list)
        return self.say.await_args_list[-1].kwargs["text"]

    async def _add(self, query: str) -> str:
        return await self._ask(f"add {query}")

    async def test_aliases_dedupe_by_identity_and_same_name_remakes_stay_distinct(
        self,
    ) -> None:
        first = await self._add("Dune")
        alias = await self._add("Arrakis")
        self.assertIn("done", first)
        self.assertIn("already_present", alias)
        self.assertEqual(len(self.store.pages), 1)

        await self._add("The Thing 1982")
        await self._add("The Thing 2011")
        entries = await self.runtime.list_watchlist()
        things = [entry for entry in entries if entry.title == "The Thing"]
        self.assertEqual({entry.imdb_id for entry in things}, {"tt0000002", "tt0000003"})
        self.assertEqual(len(self.store.pages), 3)
        self.assertTrue(
            any("Original user request: 'add Dune'" in p for p in self.judge_prompts)
        )

    async def test_unknown_legacy_exact_title_requires_clarification_without_create(
        self,
    ) -> None:
        self.store.pages["legacy-page"] = {
            "id": "legacy-page",
            "properties": _properties("Arrival"),
            "archived": False,
            "in_trash": False,
        }

        reply = await self._add("Arrival")

        self.assertIn("clarification_required", reply)
        self.assertEqual(len(self.store.pages), 1)

    async def test_unknown_legacy_alias_requires_clarification_without_create(
        self,
    ) -> None:
        self.store.pages["legacy-alias"] = {
            "id": "legacy-alias",
            "properties": _properties("Arrakis: The Desert"),
            "archived": False,
            "in_trash": False,
        }

        reply = await self._add("Dune")

        self.assertIn("clarification_required", reply)
        self.assertIn("verify legacy IMDb IDs", reply)
        self.assertEqual(self.store.create_count, 0)
        self.assertEqual(len(self.store.pages), 1)

    async def test_too_many_unknown_legacy_entries_require_verification(self) -> None:
        for index in range(26):
            page_id = f"legacy-{index}"
            self.store.pages[page_id] = {
                "id": page_id,
                "properties": _properties(f"Unindexed title {index}"),
                "archived": False,
                "in_trash": False,
            }

        reply = await self._add("Dune")

        self.assertIn("clarification_required", reply)
        self.assertIn("Over 25", reply)
        self.assertEqual(self.store.create_count, 0)
        self.assertEqual(len(self.store.pages), 26)

    async def test_unresolvable_unknown_legacy_entry_requires_verification(self) -> None:
        self.store.pages["legacy-unresolved"] = {
            "id": "legacy-unresolved",
            "properties": _properties("Unindexed title"),
            "archived": False,
            "in_trash": False,
        }

        reply = await self._add("Dune")

        self.assertIn("clarification_required", reply)
        self.assertIn("Legacy title unresolved", reply)
        self.assertEqual(self.store.create_count, 0)
        self.assertEqual(len(self.store.pages), 1)

    async def test_watched_unwatched_soft_remove_and_refresh_preserve_human_fields(
        self,
    ) -> None:
        await self._add("Dune")
        page_id = next(iter(self.store.pages))
        page = self.store.pages[page_id]

        self.assertIn("watched", await self._ask("mark watched"))
        self.assertEqual((await self.runtime.list_watchlist(unwatched=True)), [])
        self.assertIn("unwatched", await self._ask("mark unwatched"))
        self.assertEqual(
            [entry.page_id for entry in await self.runtime.list_watchlist(True)], [page_id]
        )

        page["properties"][PROP_TITLE] = _text_property("title", "My Dune Title")
        page["properties"][PROP_TYPE] = {"type": "select", "select": {"name": "TV Show"}}
        page["properties"][PROP_WATCHED] = {"type": "checkbox", "checkbox": True}
        reply = await self._ask("refresh")
        self.assertIn("done", reply)
        refreshed = page["properties"]
        self.assertEqual(
            "".join(x["plain_text"] for x in refreshed[PROP_TITLE]["title"]), "My Dune Title"
        )
        self.assertEqual(refreshed[PROP_TYPE]["select"]["name"], "TV Show")
        self.assertIs(refreshed[PROP_WATCHED]["checkbox"], True)
        self.assertEqual(refreshed[PROP_IMDB_ID]["rich_text"][0]["plain_text"], "tt0000001")

        self.assertIn("removed", await self._ask("remove"))
        self.assertTrue(page["archived"])
        self.assertEqual(await self.runtime.list_watchlist(), [])

    async def test_create_timeout_reconciles_by_imdb_id_without_replaying_post(self) -> None:
        self.store.timeout_after_create = True

        reply = await self._add("Dune")

        self.assertIn("already_present", reply)
        self.assertEqual(self.store.create_count, 1)
        entries = await self.runtime.list_watchlist()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].imdb_id, "tt0000001")

    async def test_sweep_restarts_cleanly_from_conversation_checkpoint(self) -> None:
        await self._add("Dune")
        page_id = next(iter(self.store.pages))
        self.store.pages[page_id]["properties"][PROP_STATUS] = {
            "type": "select",
            "select": {"name": "pending"},
        }
        self.omdb.searches.clear()

        summary = await self.runtime.reconcile(limit=1)

        self.assertEqual(summary.done, 1)
        self.assertEqual(self.omdb.searches, ["Dune"])
        self.assertFalse(
            any("Original user request: 'add Dune'" in p for p in self.judge_prompts[-1:])
        )


if __name__ == "__main__":
    unittest.main()

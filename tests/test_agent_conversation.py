from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from notion_db_updater.agent import build_planner
from notion_db_updater.conversation import Conversation
from notion_db_updater.evaluation import FixtureResearchClient, FixtureRuntime
from notion_db_updater.slack import SlackTransport


class ScriptedToolModel(FakeMessagesListChatModel):
    """Fake model that accepts LangChain's tool bindings and returns scripted calls."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001
        return self


def tool_call(name: str, arguments: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": call_id, "type": "tool_call"}],
    )


def reply_call(text: str, references: list[str], call_id: str) -> AIMessage:
    return tool_call("Reply", {"text": text, "references": references}, call_id)


class AgentConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = FixtureRuntime(FixtureResearchClient())
        self.models: list[ScriptedToolModel] = []
        owner = "owner"
        self.conversation = Conversation(
            self.runtime,
            str(Path(self.temp.name) / "conversation.sqlite"),
            owner,
            self._planner(
                tool_call("resolve_title", {"query": "Lone Survivor"}, "resolve-1"),
                tool_call(
                    "add_entry",
                    {"imdb_id": "tt1091191", "resolution_query": "Lone Survivor"},
                    "add-1",
                ),
                reply_call("Added Lone Survivor.", [], "reply-1"),
            ),
            research=self.runtime.research,
        )
        self.transport = object.__new__(SlackTransport)
        self.transport._runtime = cast(Any, SimpleNamespace(conversation=self.conversation))
        self.transport._tasks = set()
        self.transport._owner = self.conversation.owner
        self.transport._app = SimpleNamespace(client=AsyncMock())

    def _planner(self, *responses: AIMessage):
        model = ScriptedToolModel(responses=list(responses))
        self.models.append(model)
        return build_planner(model)

    async def _send(
        self, text: str, *, ts: str, thread_ts: str | None = None, mention: bool = False
    ) -> str:
        from unittest.mock import AsyncMock

        say = AsyncMock()
        event = {
            "text": f"<@BOT> {text}" if mention else text,
            "channel": "C",
            "user": "owner",
            "ts": ts,
            "team": "T",
        }
        if thread_ts and not mention:
            event["thread_ts"] = thread_ts
        await self.transport.handle_conversation(event, say, "T")
        if say.await_args is None:
            raise AssertionError("Slack transport produced no reply")
        return say.await_args.kwargs["text"]

    async def test_agent_resolves_and_stages_before_runtime_add(self) -> None:
        result = await self._send("Add Lone Survivor", ts="1", mention=True)
        self.assertIn("Lone Survivor", result)
        self.assertEqual(self.runtime.writes, [("add", "tt1091191")])

    async def test_agent_runs_reconcile_through_a_tool_call(self) -> None:
        from unittest.mock import AsyncMock

        self.runtime.reconcile = AsyncMock(return_value="1 entries → 1 done")
        self.conversation.planner = self._planner(
            tool_call("run_reconcile", {}, "reconcile-1"),
            reply_call("The pending Entry was enriched.", [], "reply-reconcile"),
        )

        result = await self._send("Run an enrichment sweep", ts="reconcile", mention=True)

        self.runtime.reconcile.assert_awaited_once_with()
        self.assertEqual(result, "The pending Entry was enriched.")

    async def test_agent_cannot_add_a_different_valid_identity(self) -> None:
        self.conversation.planner = self._planner(
            tool_call("resolve_title", {"query": "Lone Survivor"}, "resolve-1"),
            tool_call(
                "add_entry",
                {"imdb_id": "tt0133093", "resolution_query": "Lone Survivor"},
                "add-1",
            ),
            reply_call("Added The Matrix.", [], "reply-1"),
        )
        result = await self._send("Add Lone Survivor", ts="wrong-id", mention=True)
        self.assertIn("clarification_required", result)
        self.assertEqual(self.runtime.writes, [])

    async def test_agent_cannot_swap_ids_between_grounded_title_resolutions(self) -> None:
        self.conversation.planner = self._planner(
            tool_call("resolve_title", {"query": "Lone Survivor"}, "resolve-lone"),
            tool_call("resolve_title", {"query": "The Matrix"}, "resolve-matrix"),
            tool_call(
                "add_entry",
                {"imdb_id": "tt0133093", "resolution_query": "Lone Survivor"},
                "add-lone",
            ),
            tool_call(
                "add_entry",
                {"imdb_id": "tt1091191", "resolution_query": "The Matrix"},
                "add-matrix",
            ),
            reply_call("Added both titles.", [], "reply-batch"),
        )
        result = await self._send(
            "Add Lone Survivor and The Matrix", ts="swapped-ids", mention=True
        )
        self.assertEqual(result.count("clarification_required"), 2)
        self.assertEqual(self.runtime.writes, [])

    async def test_grounded_alias_resolution_can_add_canonical_identity(self) -> None:
        identity = {
            "imdb_id": "tt1091191",
            "title": "The Lord of the Rings",
            "year": 2001,
            "media_type": "Movie",
        }

        async def resolve_title(query: str, media_type: str | None = None) -> list[dict]:
            self.assertEqual(query, "LOTR")
            return [identity]

        async def validate_identity(imdb_id: str) -> dict:
            self.assertEqual(imdb_id, identity["imdb_id"])
            return identity

        async def add_resolved(imdb_id: str, **_kwargs: Any) -> dict[str, str]:
            self.runtime.writes.append(("add", imdb_id))
            return {"status": "done", "page_id": "fixture-lotr", "title": identity["title"]}

        self.runtime.resolve_title = resolve_title
        self.runtime.validate_identity = validate_identity
        self.runtime.add_resolved = add_resolved
        self.conversation.planner = self._planner(
            tool_call("resolve_title", {"query": "LOTR"}, "resolve-alias"),
            tool_call(
                "add_entry",
                {"imdb_id": "tt1091191", "resolution_query": "LOTR"},
                "add-alias",
            ),
            reply_call("Added The Lord of the Rings.", [], "reply-alias"),
        )
        result = await self._send("Add LOTR", ts="alias", mention=True)
        self.assertIn("The Lord of the Rings", result)
        self.assertEqual(self.runtime.writes, [("add", "tt1091191")])

    async def test_incidental_generic_query_does_not_ground_an_add(self) -> None:
        async def resolve_any_query(query: str, media_type: str | None = None) -> list[dict]:
            return [await self.runtime.validate_identity("tt1091191")]

        self.runtime.resolve_title = resolve_any_query
        self.conversation.planner = self._planner(
            tool_call("resolve_title", {"query": "movie"}, "resolve-generic"),
            tool_call(
                "add_entry",
                {"imdb_id": "tt1091191", "resolution_query": "movie"},
                "add-generic",
            ),
            reply_call("Added Lone Survivor.", [], "reply-generic"),
        )
        result = await self._send("Add a movie", ts="generic", mention=True)
        self.assertIn("clarification_required", result)
        self.assertEqual(self.runtime.writes, [])

    async def test_recommendation_references_survive_restart_and_follow_up(self) -> None:
        await self.runtime.add_resolved("tt1091191", "fixture", "C", "owner", "1", "seed")
        self.runtime.writes.clear()
        self.conversation.planner = self._planner(
            tool_call("list_watchlist", {"unwatched": True}, "list-1"),
            reply_call("1. Lone Survivor", ["fixture-tt1091191"], "reply-2"),
        )
        recommendation = await self._send(
            "Recommend something unwatched", ts="2", mention=True
        )
        self.assertIn("Lone Survivor", recommendation)

        self.conversation.planner = self._planner(
            tool_call("resolve_reference", {"index": 1}, "reference-1"),
            tool_call(
                "mark_watched",
                {"page_id": "fixture-tt1091191", "watched": True},
                "watched-1",
            ),
            reply_call("Marked Lone Survivor watched.", [], "reply-3"),
        )
        self.conversation = Conversation(
            self.runtime,
            str(Path(self.temp.name) / "conversation.sqlite"),
            "owner",
            self.conversation.planner,
            research=self.runtime.research,
        )
        self.transport._runtime.conversation = self.conversation
        result = await self._send(
            "Mark the first recommendation watched.", ts="3", thread_ts="2"
        )
        self.assertIn("Lone Survivor", result)
        self.assertTrue(self.runtime.entries["fixture-tt1091191"].watched)
        self.assertEqual(self.runtime.writes, [("watched", "fixture-tt1091191")])


if __name__ == "__main__":
    unittest.main()

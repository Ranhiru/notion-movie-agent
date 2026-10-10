from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
                tool_call("add_entry", {"imdb_id": "tt1091191"}, "add-1"),
                reply_call("Added Lone Survivor.", [], "reply-1"),
            ),
            research=self.runtime.research,
        )
        self.transport = object.__new__(SlackTransport)
        self.transport._runtime = cast(Any, SimpleNamespace(conversation=self.conversation))
        self.transport._tasks = set()

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

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from notion_db_updater.conversation import Action, Conversation, Reply
from notion_db_updater.slack import SlackTransport


class FixtureRuntime:
    def __init__(self):
        self.entries = {
            "page-existing": SimpleNamespace(
                page_id="page-existing",
                title="Existing Movie",
                imdb_id="tt0000002",
                removed=False,
                watched=False,
            )
        }
        self.writes = []

    async def list_watchlist(self, unwatched=False):
        return list(self.entries.values())

    async def validate_identity(self, imdb_id):
        if imdb_id != "tt0000001":
            raise ValueError("IMDb identity could not be verified")
        return {
            "imdb_id": imdb_id,
            "title": "Verified Movie",
            "year": 2020,
            "media_type": "movie",
        }

    async def resolve_title(self, query, media_type=None):
        if query != "Verified Movie":
            return []
        return [await self.validate_identity("tt0000001")]

    async def add_resolved(self, imdb_id, original_request, **kwargs):
        self.writes.append(("add", imdb_id))
        return {"title": "Verified Movie", "status": "added", "page_id": "page-new"}

    async def mutate_entry(self, page_id, operation, **kwargs):
        self.writes.append((operation, page_id))
        return {"title": "Existing Movie", "status": operation, "page_id": page_id}


class GuardrailConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = FixtureRuntime()
        self.planned_actions: list[Action] = []

        async def planner(messages, context):
            for action in self.planned_actions:
                if action.operation == "add" and action.imdb_id == "tt0000001":
                    await context.resolve_title("Verified Movie")
                    action = action.model_copy(update={"resolution_query": "Verified Movie"})
                context.stage(action)
            return Reply(text="Prepared")

        self.conversation = Conversation(
            self.runtime,
            str(Path(self.temp.name) / "state.sqlite"),
            "owner",
            planner,
        )
        self.transport = object.__new__(SlackTransport)
        self.transport._runtime = SimpleNamespace(conversation=self.conversation)
        self.transport._tasks = set()
        self.transport._owner = "owner"
        self.transport._app = SimpleNamespace(client=AsyncMock())

    async def mention(self, text: str):
        say = AsyncMock()
        event = {
            "text": f"<@BOT> {text}",
            "channel": "C",
            "user": "owner",
            "ts": "1",
        }
        await self.transport._handle_mention(event, say, team_id="T")
        if self.transport._tasks:
            await __import__("asyncio").gather(*self.transport._tasks)
        return say

    async def test_negated_add_requires_preview_before_any_write(self):
        self.planned_actions = [Action(operation="add", imdb_id="tt0000001")]

        reply = await self.mention("Do not add Verified Movie")

        self.assertEqual(self.runtime.writes, [])
        self.assertIn("Approve this exact action list", reply.call_args.kwargs["text"])

    async def test_negated_operation_in_mixed_request_requires_preview(self):
        self.planned_actions = [
            Action(operation="add", imdb_id="tt0000001"),
            Action(operation="remove", page_id="page-existing"),
        ]

        reply = await self.mention("Add Verified Movie but do not remove Existing Movie")

        self.assertEqual(self.runtime.writes, [])
        self.assertIn("Approve this exact action list", reply.call_args.kwargs["text"])

    async def test_hypothetical_add_requires_preview_before_any_write(self):
        self.planned_actions = [Action(operation="add", imdb_id="tt0000001")]

        reply = await self.mention("I'm asking whether to add Verified Movie")

        self.assertEqual(self.runtime.writes, [])
        self.assertIn("Approve this exact action list", reply.call_args.kwargs["text"])

    async def test_invalid_action_does_not_block_valid_sibling(self):
        self.planned_actions = [
            Action(operation="add", imdb_id="tt0000001"),
            Action(operation="add", imdb_id="not-an-imdb-id"),
        ]

        reply = await self.mention("Add Verified Movie and Unknown Movie")

        self.assertEqual(self.runtime.writes, [("add", "tt0000001")])
        response = reply.call_args.kwargs["text"]
        self.assertIn("Verified Movie: added", response)
        self.assertIn("IMDb Identity", response)

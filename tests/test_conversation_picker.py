from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from notion_db_updater.conversation import Action, Conversation, Reply
from notion_db_updater.slack import SlackTransport


class ConversationPickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.candidates = [
            {
                "title": "Dark Matter",
                "year": 2024,
                "media_type": "TV Show",
                "imdb_id": "tt19231492",
                "poster": "https://example.com/poster.jpg",
            },
            {
                "title": "Dark Matter",
                "year": 2015,
                "media_type": "TV Show",
                "imdb_id": "tt4159076",
            },
        ]
        self.runtime = SimpleNamespace(
            resolve_title=AsyncMock(return_value=self.candidates),
            list_watchlist=AsyncMock(return_value=[]),
            validate_identity=AsyncMock(return_value=self.candidates[0]),
            add_resolved=AsyncMock(
                return_value={"status": "done", "title": "Dark Matter", "page_id": "page-1"}
            ),
        )

        async def planner(messages, context):
            if "I choose IMDb" in messages[-1]["content"]:
                identity = messages[-1]["content"].rsplit("I choose IMDb ", 1)[1].rstrip(".")
                context.stage(Action(operation="add", imdb_id=identity))
                return Reply(text="Selected Dark Matter.")
            await context.resolve_title("Dark Matter")
            return Reply(text="Which Dark Matter?", disambiguations=["Dark Matter"])

        self.planner = planner
        self.db = str(Path(self.temp.name) / "conversation.sqlite")
        self.conversation = Conversation(self.runtime, self.db, planner)
        self.event = {
            "text": "<@BOT> Add Dark Matter",
            "user": "U",
            "channel": "C",
            "ts": "1",
            "team": "T",
        }

    async def test_clarification_renders_existing_card_without_writing(self):
        transport = object.__new__(SlackTransport)
        transport._runtime = SimpleNamespace(conversation=self.conversation)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "progress"}
        transport._app = SimpleNamespace(client=client)
        await transport.handle_conversation(self.event, AsyncMock(), "T")
        blocks = client.chat_update.await_args.kwargs["blocks"]
        self.assertTrue(any(b["type"] == "actions" for b in blocks))
        self.assertTrue(any(b.get("accessory", {}).get("type") == "image" for b in blocks))
        self.assertTrue(any(b["type"] == "input" for b in blocks))
        self.runtime.add_resolved.assert_not_awaited()

    async def test_selection_survives_restart_and_replay(self):
        await self.conversation.handle(self.event, "T")
        choice = self.conversation.disambiguations(self.event, "T")[0]
        restarted = Conversation(self.runtime, self.db, self.planner)
        event = self.event | {"thread_ts": "1"}
        first = await restarted.choose_candidate(
            choice["request_key"], choice["token"], "tt19231492", event, "T"
        )
        second = await restarted.choose_candidate(
            choice["request_key"], choice["token"], "tt19231492", event, "T"
        )
        self.assertEqual(first, second)
        self.runtime.add_resolved.assert_awaited_once()

    async def test_selection_rejects_wrong_thread_expiry_and_unknown_candidate(self):
        await self.conversation.handle(self.event, "T")
        choice = self.conversation.disambiguations(self.event, "T")[0]
        for event, identity, now in (
            (self.event | {"thread_ts": "other"}, "tt19231492", None),
            (self.event | {"thread_ts": "1"}, "tt9999999", None),
            (self.event | {"thread_ts": "1"}, "tt19231492", 99999999999),
        ):
            with (
                patch("notion_db_updater.conversation.time.time", return_value=now)
                if now
                else nullcontext()
            ):
                result = await self.conversation.choose_candidate(
                    choice["request_key"], choice["token"], identity, event, "T"
                )
            self.assertNotIn("done", result)
        self.runtime.add_resolved.assert_not_awaited()

    async def test_card_button_round_trip_through_slack_handler(self):
        transport = object.__new__(SlackTransport)
        transport._runtime = SimpleNamespace(conversation=self.conversation)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "card"}
        transport._app = SimpleNamespace(client=client)
        await transport.handle_conversation(self.event, AsyncMock(), "T")
        blocks = client.chat_update.await_args.kwargs["blocks"]
        action = next(b for b in blocks if b["type"] == "actions")["elements"][0]
        body = {
            "channel": {"id": "C"},
            "user": {"id": "U"},
            "team": {"id": "T"},
            "message": {"ts": "card"},
        }
        await transport._handle_conversation_pick(body, action, client)
        self.runtime.add_resolved.assert_awaited_once()
        self.assertIn("done", client.chat_update.await_args.kwargs["text"])
        self.assertEqual(client.chat_update.await_args.kwargs["blocks"], [])

    async def test_manual_input_resolves_an_identity_outside_candidates(self):
        await self.conversation.handle(self.event, "T")
        choice = self.conversation.disambiguations(self.event, "T")[0]
        transport = object.__new__(SlackTransport)
        transport._runtime = SimpleNamespace(conversation=self.conversation)
        client = AsyncMock()
        transport._app = SimpleNamespace(client=client)
        body = {
            "channel": {"id": "C"},
            "user": {"id": "U"},
            "team": {"id": "T"},
            "message": {"ts": "card"},
        }
        action = {
            "action_id": "conversation:manual",
            "block_id": json.dumps(
                {
                    "request_key": choice["request_key"],
                    "token": choice["token"],
                    "thread_ts": "1",
                }
            ),
            "value": "https://www.imdb.com/title/tt0416675/",
        }
        self.runtime.validate_identity.return_value = self.candidates[0] | {
            "imdb_id": "tt0416675"
        }
        await transport._handle_conversation_pick(body, action, client)
        self.runtime.validate_identity.assert_awaited_with("tt0416675")
        self.assertEqual(self.runtime.add_resolved.await_args.args[0], "tt0416675")

    async def test_unresolved_choice_keeps_bulk_plan_together(self):
        async def planner(messages, context):
            if "I choose IMDb" in messages[-1]["content"]:
                context.stage(Action(operation="add", imdb_id="tt19231492"))
                return Reply(text="Ready.")
            for identity in ("tt0000001", "tt0000002", "tt0000003"):
                context.stage(Action(operation="add", imdb_id=identity))
            await context.resolve_title("Dark Matter")
            return Reply(text="Choose Dark Matter.", disambiguations=["Dark Matter"])

        async def validate(identity):
            return self.candidates[0] | {"imdb_id": identity}

        self.runtime.validate_identity.side_effect = validate
        self.conversation.planner = planner
        await self.conversation.handle(self.event, "T")
        self.runtime.add_resolved.assert_not_awaited()
        choice = self.conversation.disambiguations(self.event, "T")[0]
        response = await self.conversation.choose_candidate(
            choice["request_key"],
            choice["token"],
            "tt19231492",
            self.event | {"thread_ts": "1"},
            "T",
        )
        self.assertTrue(response.startswith("Approve this exact action list"))
        self.assertIn("4. add", response)
        self.runtime.add_resolved.assert_not_awaited()

    async def test_superseded_card_and_changed_replay_are_rejected(self):
        await self.conversation.handle(self.event, "T")
        choice = self.conversation.disambiguations(self.event, "T")[0]
        await self.conversation.handle(self.event | {"ts": "2", "thread_ts": "1"}, "T")
        response = await self.conversation.choose_candidate(
            choice["request_key"],
            choice["token"],
            "tt19231492",
            self.event | {"thread_ts": "1"},
            "T",
        )
        self.assertIn("superseded", response)
        self.runtime.add_resolved.assert_not_awaited()
        current_event = self.event | {"ts": "2", "thread_ts": "1"}
        choice = self.conversation.disambiguations(current_event, "T")[0]
        await self.conversation.choose_candidate(
            choice["request_key"], choice["token"], "tt19231492", current_event, "T"
        )
        response = await self.conversation.choose_candidate(
            choice["request_key"], choice["token"], "tt4159076", current_event, "T"
        )
        self.assertIn("already been used", response)
        self.runtime.add_resolved.assert_awaited_once()

    async def test_uncertain_staged_candidate_is_not_carried_into_selection(self):
        original_planner = self.planner

        async def planner(messages, context):
            if "I choose IMDb" not in messages[-1]["content"]:
                context.stage(Action(operation="add", imdb_id="tt4159076"))
            return await original_planner(messages, context)

        self.conversation.planner = planner
        await self.conversation.handle(self.event, "T")
        self.runtime.add_resolved.assert_not_awaited()
        choice = self.conversation.disambiguations(self.event, "T")[0]
        await self.conversation.choose_candidate(
            choice["request_key"],
            choice["token"],
            "tt19231492",
            self.event | {"thread_ts": "1"},
            "T",
        )
        self.runtime.add_resolved.assert_awaited_once()
        self.assertEqual(self.runtime.add_resolved.await_args.args[0], "tt19231492")

    async def test_missing_candidates_offer_manual_input(self):
        self.runtime.resolve_title.return_value = []
        transport = object.__new__(SlackTransport)
        transport._runtime = SimpleNamespace(conversation=self.conversation)
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "progress"}
        transport._app = SimpleNamespace(client=client)
        await transport.handle_conversation(self.event, AsyncMock(), "T")
        blocks = client.chat_update.await_args.kwargs["blocks"]
        self.assertFalse(any(b["type"] == "actions" for b in blocks))
        self.assertTrue(any(b["type"] == "input" for b in blocks))

    async def test_import_selection_still_requires_preview_approval(self):
        self.event["text"] = "<@BOT> Add Dark Matter from https://example.com/list"
        await self.conversation.handle(self.event, "T")
        choice = self.conversation.disambiguations(self.event, "T")[0]
        response = await self.conversation.choose_candidate(
            choice["request_key"],
            choice["token"],
            "tt19231492",
            self.event | {"thread_ts": "1"},
            "T",
        )
        self.assertTrue(response.startswith("Approve this exact action list"))
        self.runtime.add_resolved.assert_not_awaited()

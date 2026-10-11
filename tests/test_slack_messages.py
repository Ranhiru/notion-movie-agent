from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from notion_db_updater.slack import SlackTransport


class SlackMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = object.__new__(SlackTransport)
        self.transport._channel = "#notion-movie-db"
        self.transport._channel_names = {}
        self.transport._runtime = SimpleNamespace(conversation=object())
        client = AsyncMock()
        client.conversations_info.return_value = {"channel": {"name": "notion-movie-db"}}
        self.listeners = {}

        def event(name):
            def register(handler):
                self.listeners[name] = handler
                return handler

            return register

        self.transport._app = SimpleNamespace(
            client=client, event=event, action=lambda name: lambda handler: handler
        )
        self.transport._schedule_conversation = Mock()
        self.transport._register()
        self.event = {
            "channel": "C123",
            "ts": "1",
            "user": "U123",
            "text": "recommend a movie",
        }
        self.say = AsyncMock()

    async def send(self, event, kind="message"):
        await self.listeners[kind]({"team_id": "T123"}, event, self.say)

    async def test_root_starts_without_mention_and_name_lookup_is_cached(self):
        await self.send(self.event)
        await self.send(self.event | {"ts": "2"})
        self.assertEqual(self.transport._schedule_conversation.call_count, 2)
        self.transport._schedule_conversation.assert_called_with(
            self.event | {"ts": "2"}, self.say, "T123"
        )
        self.transport._app.client.conversations_info.assert_awaited_once_with(channel="C123")

    async def test_channel_id_needs_no_lookup_and_other_roots_are_ignored(self):
        self.transport._channel = "C123"
        await self.send(self.event | {"channel": "C999"})
        self.transport._schedule_conversation.assert_not_called()
        await self.send(self.event)
        self.transport._schedule_conversation.assert_called_once()
        self.transport._app.client.conversations_info.assert_not_awaited()

    async def test_other_channel_name_is_ignored(self):
        self.transport._app.client.conversations_info.return_value = {
            "channel": {"name": "other"}
        }
        await self.send(self.event)
        self.transport._schedule_conversation.assert_not_called()

    async def test_lookup_failure_skips_root_and_can_retry(self):
        client = self.transport._app.client
        client.conversations_info.side_effect = RuntimeError("missing_scope")
        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await self.send(self.event)
        self.transport._schedule_conversation.assert_not_called()
        client.conversations_info.side_effect = None
        await self.send(self.event)
        self.transport._schedule_conversation.assert_called_once()

    async def test_tagged_root_has_only_one_owner_in_either_delivery_order(self):
        event = self.event | {"text": "<@BOT> recommend a movie"}
        for order in (("message", "app_mention"), ("app_mention", "message")):
            with self.subTest(order=order):
                self.transport._schedule_conversation.reset_mock()
                for kind in order:
                    await self.send(event, kind)
                self.transport._schedule_conversation.assert_called_once_with(
                    event, self.say, "T123"
                )

    async def test_thread_and_mentions_elsewhere_keep_existing_routing(self):
        self.transport._channel = "C999"
        await self.send(self.event, "app_mention")
        await self.send(self.event | {"thread_ts": "0"})
        tagged_thread = self.event | {"thread_ts": "0", "text": "<@BOT> continue"}
        await self.send(tagged_thread)
        self.assertEqual(self.transport._schedule_conversation.call_count, 2)
        await self.send(tagged_thread, "app_mention")
        self.assertEqual(self.transport._schedule_conversation.call_count, 3)
        self.transport._app.client.conversations_info.assert_not_awaited()

    async def test_bot_subtype_and_malformed_messages_are_ignored_before_lookup(self):
        for event in (
            self.event | {"bot_id": "B123"},
            self.event | {"subtype": "message_changed"},
            self.event | {"subtype": "message_deleted"},
            self.event | {"text": ""},
            self.event | {"channel": ""},
            self.event | {"ts": ""},
        ):
            with self.subTest(event=event):
                await self.send(event)
        self.transport._schedule_conversation.assert_not_called()
        self.transport._app.client.conversations_info.assert_not_awaited()

    async def test_disabled_conversation_does_not_look_up_roots(self):
        self.transport._runtime.conversation = None
        await self.send(self.event)
        self.transport._schedule_conversation.assert_not_called()
        self.transport._app.client.conversations_info.assert_not_awaited()

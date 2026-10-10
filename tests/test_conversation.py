from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from notion_db_updater.config import Settings
from notion_db_updater.conversation import Action, Conversation, Reply
from notion_db_updater.research import ResearchClient
from notion_db_updater.slack import SlackTransport


class FixtureRuntime:
    title_by_id = {
        "tt0000001": "Lone Survivor",
        "tt0000002": "Black Hawk Down",
        "tt0000003": "Jaws",
        "tt0000004": "Alien",
        "tt0000009": "Movie nine",
    }

    def __init__(self):
        self.entries = {}
        self.writes = []

    async def list_watchlist(self, unwatched=False):
        return list(self.entries.values())

    async def validate_identity(self, imdb_id):
        return {
            "imdb_id": imdb_id,
            "title": f"Movie {imdb_id}",
            "year": 2020,
            "media_type": "Movie",
        }

    async def resolve_title(self, query, media_type=None):
        imdb_id = next(
            (
                key
                for key, title in self.title_by_id.items()
                if title.casefold() == query.casefold()
            ),
            None,
        )
        return [await self.validate_identity(imdb_id)] if imdb_id else []

    async def add_resolved(self, imdb_id, original_request, **kwargs):
        for entry in self.entries.values():
            if entry.imdb_id == imdb_id:
                return {
                    "status": "already_present",
                    "page_id": entry.page_id,
                    "title": entry.title,
                }
        page_id = f"page-{imdb_id}"
        self.entries[page_id] = SimpleNamespace(
            page_id=page_id,
            imdb_id=imdb_id,
            title=f"Movie {imdb_id}",
            watched=False,
            removed=False,
            plot="A war story",
            genre="Drama",
            imdb_rating=8,
            rt_critic=80,
            rt_audience=90,
            status="done",
            media_type="Movie",
        )
        self.writes.append(("add", imdb_id))
        return {"status": "done", "page_id": page_id, "title": f"Movie {imdb_id}"}

    async def mutate_entry(self, page_id, operation, **kwargs):
        entry = self.entries[page_id]
        self.writes.append((operation, page_id))
        if operation == "remove":
            entry.removed = True
        elif operation in {"watched", "unwatched"}:
            entry.watched = operation == "watched"
        return {"status": operation, "page_id": page_id, "title": entry.title}


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def approval_token(reply):
        return json.loads(reply.call_args.kwargs["blocks"][-1]["elements"][0]["value"])[
            "token"
        ]

    async def button_plan(self, decision="approve", selected=(1, 2, 3, 4)):
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 5)]
        )
        reply = await self.mention("add Lone Survivor, Black Hawk Down, Jaws and Alien")
        blocks = reply.call_args.kwargs["blocks"]
        self.assertEqual(blocks[0]["text"]["text"], "Review Watchlist changes")
        action = next(
            a for a in blocks[-1]["elements"] if a["action_id"] == f"watchlist:{decision}"
        )
        body = {
            "user": {"id": "owner"},
            "channel": {"id": "C"},
            "team": {"id": "T"},
            "message": {"ts": "preview-1"},
            "state": {
                "values": {
                    "watchlist_selection": {
                        "watchlist:selection": {
                            "selected_options": [{"value": str(n)} for n in selected],
                        }
                    }
                }
            },
        }
        return body, action

    async def test_approve_button_applies_only_selection_once(self):
        body, action = await self.button_plan(selected=(2, 4))
        client = self.transport._app.client
        await self.transport._handle_approval(body, action, client)
        await self.transport._handle_approval(body, action, client)
        self.assertEqual(self.runtime.writes, [("add", "tt0000002"), ("add", "tt0000004")])
        self.assertNotIn(
            "actions", [b["type"] for b in client.chat_update.call_args.kwargs["blocks"]]
        )
        self.assertEqual(self.scripts, [])

    async def test_deny_button_persists_and_cannot_be_approved_after_restart(self):
        body, action = await self.button_plan(decision="deny")
        await self.transport._handle_approval(body, action, self.transport._app.client)
        self.assertEqual(self.runtime.writes, [])
        self.transport._runtime.conversation = Conversation(
            self.runtime, self.conversation.db_path, "owner", self.planner
        )
        await self.transport._handle_approval(
            body, action | {"action_id": "watchlist:approve"}, self.transport._app.client
        )
        self.assertEqual(self.runtime.writes, [])
        self.assertEqual(await self.transport._runtime.conversation.recover(), [])

    async def test_buttons_reject_other_users_empty_selection_and_expired_plan(self):
        body, action = await self.button_plan(selected=())
        client = self.transport._app.client
        await self.transport._handle_approval(body | {"user": {"id": "other"}}, action, client)
        client.chat_update.assert_not_awaited()
        await self.transport._handle_approval(body, action, client)
        client.chat_postEphemeral.assert_awaited_once()
        pending = self.conversation._load("request:T:C:1")
        pending["expires"] = 0
        self.conversation._save("request:T:C:1", pending)
        body.pop("state")
        await self.transport._handle_approval(body, action, client)
        self.assertEqual(self.runtime.writes, [])
        self.assertIn("expired", client.chat_update.call_args.kwargs["text"])

    async def test_button_token_is_bound_to_its_original_thread(self):
        body, action = await self.button_plan()
        value = json.loads(action["value"])
        value["thread_ts"] = "another-thread"
        await self.transport._handle_approval(
            body, action | {"value": json.dumps(value)}, self.transport._app.client
        )
        self.assertEqual(self.runtime.writes, [])

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = FixtureRuntime()
        self.scripts = []

        async def planner(messages, context):
            script = self.scripts.pop(0)
            actions, reply = (
                script if isinstance(script, tuple) else (script, Reply(text="Prepared"))
            )
            if "http://" in context.request["text"] or "https://" in context.request["text"]:
                context.request["imported"] = True
            for action in actions:
                if action.operation == "add" and not context.request.get("imported"):
                    query = action.resolution_query or self.runtime.title_by_id.get(
                        action.imdb_id
                    )
                    if query:
                        await context.resolve_title(query)
                        action = action.model_copy(update={"resolution_query": query})
                context.stage(action)
            return reply

        self.planner = planner
        self.conversation = Conversation(
            self.runtime, str(Path(self.temp.name) / "state.sqlite"), "owner", planner
        )
        self.transport = object.__new__(SlackTransport)
        self.transport._runtime = SimpleNamespace(conversation=self.conversation)
        self.transport._tasks = set()
        self.transport._owner = "owner"
        self.transport._app = SimpleNamespace(client=AsyncMock())

    async def mention(self, text, *, ts="1", user="owner", thread_ts=None):
        say = AsyncMock()
        event = {"text": f"<@BOT> {text}", "channel": "C", "user": user, "ts": ts}
        if thread_ts:
            event["thread_ts"] = thread_ts
        await self.transport._handle_mention(event, say, team_id="T")
        if self.transport._tasks:
            await __import__("asyncio").gather(*self.transport._tasks)
        return say

    async def test_two_named_movies_are_added_as_separate_entries(self):
        self.scripts.append(
            [
                Action(operation="add", imdb_id="tt0000001"),
                Action(operation="add", imdb_id="tt0000002"),
            ]
        )
        reply = await self.mention("add Lone Survivor and Black Hawk Down")
        self.assertEqual(self.runtime.writes, [("add", "tt0000001"), ("add", "tt0000002")])
        self.assertIn("Movie tt0000001", reply.call_args.kwargs["text"])
        self.assertEqual(reply.call_args.kwargs["thread_ts"], "1")

    async def test_polite_single_add_does_not_require_redundant_approval(self):
        self.scripts.append([Action(operation="add", imdb_id="tt0000001")])
        reply = await self.mention(
            "Can you add Lone Survivor to my list ? I want to watch the latest season"
        )
        self.assertEqual(self.runtime.writes, [("add", "tt0000001")])
        self.assertNotIn("approve", reply.call_args.kwargs["text"])

    def test_polite_requests_preserve_ambiguous_and_negated_guards(self):
        for text in (
            "Can you add the Reacher to my list ? I want to watch the latest season",
            "Could you add Reacher?",
            "Would you please add Reacher?",
        ):
            self.assertTrue(Conversation._explicit_action(text, "add"))
        for text in (
            "Can you add Reacher if it is good?",
            "Could you not add Reacher?",
            "Can you recommend something to add?",
            "Should I add Reacher?",
        ):
            self.assertFalse(Conversation._explicit_action(text, "add"))

    async def test_four_separate_tool_calls_wait_for_request_wide_approval(self):
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 5)]
        )
        reply = await self.mention("add Lone Survivor, Black Hawk Down, Jaws and Alien")
        self.assertEqual(self.runtime.writes, [])
        preview = reply.call_args.kwargs["text"]
        self.assertIn("Approve", preview)
        token = self.approval_token(reply)
        reply = await self.mention(f"approve {token}", ts="2", thread_ts="1")
        self.assertEqual(len(self.runtime.writes), 4)

    async def test_unauthorized_caller_is_ignored(self):
        reply = await self.mention("remove everything", user="stranger")
        reply.assert_not_called()
        self.assertEqual(self.runtime.writes, [])

    async def test_url_import_always_previews_and_can_select_one_entry(self):
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 3)]
        )
        reply = await self.mention("add movies from https://example.com/top10")
        self.assertEqual(self.runtime.writes, [])
        token = self.approval_token(reply)
        await self.mention(f"approve {token} 2", ts="2", thread_ts="1")
        self.assertEqual(self.runtime.writes, [("add", "tt0000002")])

    async def test_duplicate_delivery_does_not_repeat_writes(self):
        self.scripts.append([Action(operation="add", imdb_id="tt0000001")])
        await self.mention("add Lone Survivor")
        await self.mention("add Lone Survivor")
        self.assertEqual(self.runtime.writes, [("add", "tt0000001")])

    async def test_plain_thread_reference_survives_restart(self):
        await self.runtime.add_resolved("tt0000001", "fixture")
        self.runtime.writes.clear()
        self.scripts.append(([], Reply(text="1. A movie", references=["page-tt0000001"])))
        await self.mention("recommend something unwatched")
        self.transport._runtime.conversation = Conversation(
            self.runtime, self.conversation.db_path, "owner", self.planner
        )
        self.scripts.append([Action(operation="watched", reference=1)])
        reply = AsyncMock()
        await self.transport.handle_conversation(
            {
                "channel": "C",
                "ts": "2",
                "thread_ts": "1",
                "user": "owner",
                "text": "mark the first one watched",
            },
            reply,
            "T",
        )
        self.assertTrue(self.runtime.entries["page-tt0000001"].watched)
        self.assertEqual(reply.call_args.kwargs["thread_ts"], "1")

    async def test_pending_preview_survives_restart_without_replanning(self):
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 5)]
        )
        reply = await self.mention("add Lone Survivor, Black Hawk Down, Jaws and Alien")
        token = self.approval_token(reply)
        self.transport._runtime.conversation = Conversation(
            self.runtime, self.conversation.db_path, "owner", self.planner
        )
        await self.mention(f"approve {token}", ts="2", thread_ts="1")
        self.assertEqual(len(self.runtime.writes), 4)

    async def test_partial_failure_preserves_success_and_summarizes_each_entry(self):
        original = self.runtime.add_resolved

        async def fail_second(imdb_id, original_request, **kwargs):
            if imdb_id == "tt0000002":
                raise RuntimeError("fixture failure")
            return await original(imdb_id, original_request, **kwargs)

        self.runtime.add_resolved = fail_second
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 4)]
        )
        with self.assertLogs("notion_db_updater.conversation", level="ERROR"):
            reply = await self.mention("add Lone Survivor, Black Hawk Down and Jaws")
        self.assertEqual(self.runtime.writes, [("add", "tt0000001"), ("add", "tt0000003")])
        self.assertIn("Movie tt0000002: failed", reply.call_args.kwargs["text"])

    async def test_restart_after_remote_write_does_not_repeat_completed_effect(self):
        import asyncio

        original = self.runtime.add_resolved

        async def interrupted(imdb_id, original_request, **kwargs):
            await original(imdb_id, original_request, **kwargs)
            raise asyncio.CancelledError

        self.runtime.add_resolved = interrupted
        self.scripts.append([Action(operation="add", imdb_id="tt0000001")])
        with self.assertRaises(asyncio.CancelledError):
            await self.mention("add Lone Survivor")
        self.runtime.add_resolved = original
        self.transport._runtime.conversation = Conversation(
            self.runtime, self.conversation.db_path, "owner", self.planner
        )
        reply = await self.mention("add Lone Survivor")
        self.assertEqual(self.runtime.writes, [("add", "tt0000001")])
        self.assertIn("recovered", reply.call_args.kwargs["text"])

    async def test_expired_approval_cannot_write(self):
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 5)]
        )
        with patch("notion_db_updater.conversation.time.time", return_value=100):
            reply = await self.mention("add Lone Survivor, Black Hawk Down, Jaws and Alien")
        token = self.approval_token(reply)
        with patch("notion_db_updater.conversation.time.time", return_value=1901):
            reply = await self.mention(f"approve {token}", ts="2", thread_ts="1")
        self.assertEqual(self.runtime.writes, [])
        self.assertIn("expired", reply.call_args.kwargs["text"])

    async def test_research_content_cannot_authorize_an_unrequested_write(self):
        await self.runtime.add_resolved("tt0000001", "fixture")
        self.runtime.writes.clear()
        self.scripts.append([Action(operation="remove", page_id="page-tt0000001")])
        reply = await self.mention("give me some unwatched recommendations")
        self.assertEqual(self.runtime.writes, [])
        self.assertIn("Approve", reply.call_args.kwargs["text"])

    async def test_search_limit_is_enforced_and_reported_by_application(self):
        research = SimpleNamespace(search=AsyncMock(return_value=[]))
        self.conversation.research = research

        async def planner(messages, context):
            for _ in range(4):
                await context.search("war movies")
            return Reply(text="No extra evidence available.")

        self.conversation.planner = planner
        reply = await self.mention("find some war movies")
        self.assertEqual(research.search.await_count, 3)
        self.assertIn("Search limit reached", reply.call_args.kwargs["text"])

    async def test_page_redirect_to_private_network_is_blocked_at_conversation_boundary(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            research = ResearchClient(
                Settings(NOTION_MOVIE_DB_TOKEN="fixture", _env_file=None), page_http=http
            )
            self.addAsyncCleanup(research.aclose)
            self.conversation.research = research

            async def planner(messages, context):
                body = await context.fetch_page("https://93.184.216.34/list")
                context.stage(Action(operation="add", imdb_id="tt0000001"))
                return Reply(text=body)

            self.conversation.planner = planner
            with self.assertLogs("notion_db_updater.conversation", level="ERROR"):
                reply = await self.mention("add movies from https://93.184.216.34/list")
            self.assertEqual(len(requests), 1)
            self.assertEqual(self.runtime.writes, [])
            self.assertIn("could not be read safely", reply.call_args.kwargs["text"])

    async def test_pending_approval_is_single_use_and_replaced_plans_invalidate_it(self):
        self.scripts.append(
            [Action(operation="add", imdb_id=f"tt000000{i}") for i in range(1, 5)]
        )
        preview = await self.mention("add Lone Survivor, Black Hawk Down, Jaws and Alien")
        token = self.approval_token(preview)
        self.scripts.append([Action(operation="add", imdb_id="tt0000009")])
        await self.mention("actually add only Movie nine", ts="2", thread_ts="1")
        reply = await self.mention(f"approve {token}", ts="3", thread_ts="1")
        self.assertEqual(self.runtime.writes, [("add", "tt0000009")])
        self.assertIn("invalid", reply.call_args.kwargs["text"])

    async def test_startup_recovery_completes_unstarted_actions_without_repeating_effects(
        self,
    ):
        import asyncio

        original = self.runtime.add_resolved

        async def interrupted(imdb_id, original_request, **kwargs):
            await original(imdb_id, original_request, **kwargs)
            raise asyncio.CancelledError

        self.runtime.add_resolved = interrupted
        self.scripts.append(
            [
                Action(operation="add", imdb_id="tt0000001"),
                Action(operation="add", imdb_id="tt0000002"),
            ]
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.mention("add Lone Survivor and Black Hawk Down")
        self.runtime.add_resolved = original
        restarted = Conversation(
            self.runtime, self.conversation.db_path, "owner", self.planner
        )
        recovered = await restarted.recover()
        self.assertEqual(self.runtime.writes, [("add", "tt0000001"), ("add", "tt0000002")])
        self.assertEqual(recovered[0]["thread_ts"], "1")
        self.assertIn("recovered", recovered[0]["text"])
        self.assertEqual(await restarted.recover(), [])

    async def test_added_summary_numbers_survive_restart_for_followup(self):
        self.scripts.append(
            [
                Action(operation="add", imdb_id="tt0000001"),
                Action(operation="add", imdb_id="tt0000002"),
            ]
        )
        reply = await self.mention("add Lone Survivor and Black Hawk Down")
        self.assertIn("2. Movie tt0000002", reply.call_args.kwargs["text"])
        self.transport._runtime.conversation = Conversation(
            self.runtime, self.conversation.db_path, "owner", self.planner
        )
        self.scripts.append([Action(operation="watched", reference=2)])
        await self.mention("mark the second watched", ts="2", thread_ts="1")
        self.assertTrue(self.runtime.entries["page-tt0000002"].watched)
        self.assertFalse(self.runtime.entries["page-tt0000001"].watched)

    async def test_invalid_displayed_reference_does_not_shift_numbered_targets(self):
        await self.runtime.add_resolved("tt0000001", "fixture")
        self.runtime.writes.clear()
        self.scripts.append(
            (
                [],
                Reply(
                    text="1. Missing\n2. Movie one", references=["missing", "page-tt0000001"]
                ),
            )
        )
        with self.assertLogs("notion_db_updater.conversation", level="ERROR"):
            reply = await self.mention("give me recommendations")
        self.assertIn("couldn't safely prepare", reply.call_args.kwargs["text"])
        self.assertEqual(self.runtime.writes, [])

    async def test_read_only_reply_is_not_recovered_as_unfinished_execution(self):
        self.scripts.append(([], Reply(text="No unwatched entries.")))
        await self.mention("give me recommendations")
        self.assertEqual(await self.conversation.recover(), [])

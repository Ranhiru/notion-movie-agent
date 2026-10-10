from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

from slack_sdk.errors import SlackApiError

from notion_db_updater.app import Runtime
from notion_db_updater.conversation import RequestTools
from notion_db_updater.models import Entry, enrichment_properties
from notion_db_updater.slack import SlackTransport


def _entry(title: str = "Dune") -> Entry:
    return Entry(
        page_id="page-1",
        title=title,
        media_type=None,
        imdb_rating=None,
        rt_critic=None,
        rt_audience=None,
        plot=None,
        genre=None,
        status="pending",
    )


class _FakeNotion:
    async def create_entry(self, title: str) -> Entry:
        return _entry(title)


class _FakeGraph:
    def __init__(self) -> None:
        self.initial_state: dict | None = None

    async def astream(self, initial_state: dict, **kwargs):
        self.initial_state = initial_state
        yield {"read_page": {"entry": _entry()}}
        yield {"omdb_search": {"candidates": []}}
        yield {"judge": {"status": "done"}}
        yield {"notify": {}}

    async def aget_state(self, config: dict):
        return SimpleNamespace(values={"status": "done", "omdb_title": "Dune", "year": 2021})


class SlackProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_progress_covers_real_work_and_does_not_report_blocked_calls(self):
        progress = AsyncMock()
        runtime = SimpleNamespace(
            list_watchlist=AsyncMock(return_value=[]),
            resolve_title=AsyncMock(return_value=[]),
            reconcile=AsyncMock(return_value="Done"),
        )
        research = SimpleNamespace(
            search=AsyncMock(return_value=[]), fetch_page=AsyncMock(return_value="Page")
        )
        tools = RequestTools(
            runtime=runtime,
            request={"text": "https://example.com/movies"},
            references=[],
            research=research,
            progress=progress,
            max_searches=1,
        )
        await tools.watchlist()
        await tools.resolve_title("Reacher")
        await tools.reconcile()
        await tools.search("Reacher")
        await tools.search("Blocked second query")
        await tools.fetch_page("https://example.com/movies")
        await tools.fetch_page("https://example.com/movies")
        self.assertEqual(progress.await_count, 5)
        self.assertIn("IMDb match for Reacher", progress.await_args_list[1].args[0])
        research.search.assert_awaited_once_with("Reacher")
        research.fetch_page.assert_awaited_once()

    async def test_visible_progress_is_cleaned_up_on_ignore_failure_and_cancellation(self):
        for outcome in (None, RuntimeError("Planner failed"), asyncio.CancelledError()):
            with self.subTest(outcome=outcome):
                transport = object.__new__(SlackTransport)
                transport._owner = "U123"
                client = AsyncMock()
                client.chat_postMessage.return_value = {"ts": "progress-1"}
                transport._app = SimpleNamespace(client=client)
                handle = AsyncMock(return_value=outcome)
                if isinstance(outcome, BaseException):
                    handle.side_effect = outcome
                transport._runtime = SimpleNamespace(
                    conversation=SimpleNamespace(handle=handle)
                )
                event = {"user": "U123", "channel": "C123", "ts": "1", "text": "hello"}
                if isinstance(outcome, BaseException):
                    with self.assertRaises(type(outcome)):
                        await transport.handle_conversation(event, AsyncMock(), "T123")
                    self.assertNotIn("Working", client.chat_update.await_args.kwargs["text"])
                else:
                    await transport.handle_conversation(event, AsyncMock(), "T123")
                    client.chat_delete.assert_awaited_once_with(
                        channel="C123", ts="progress-1"
                    )
                self.assertEqual(client.api_call.await_args.kwargs["json"]["status"], "active")

    async def test_working_message_and_real_tool_progress_precede_final_reply(self):
        transport = object.__new__(SlackTransport)
        transport._owner = "U123"
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "progress-1"}
        transport._app = SimpleNamespace(client=client)

        async def search(query):
            self.assertIn(
                "Searching the web for Reacher", client.chat_update.await_args.kwargs["text"]
            )
            return []

        async def handle(event, team, *, progress):
            client.chat_postMessage.assert_awaited_once_with(
                channel="C123",
                thread_ts="1",
                text="💭 Working on your request…",
            )
            tools = RequestTools(
                runtime=SimpleNamespace(),
                request={"text": event["text"]},
                references=[],
                research=SimpleNamespace(search=search),
                progress=progress,
            )
            await tools.search("Reacher latest season")
            return "Here is the result."

        transport._runtime = SimpleNamespace(conversation=SimpleNamespace(handle=handle))
        say = AsyncMock()
        event = {
            "user": "U123",
            "channel": "C123",
            "ts": "2",
            "thread_ts": "1",
            "text": "Check Reacher",
        }
        await transport.handle_conversation(event, say, "T123")
        self.assertEqual(client.chat_update.await_args.kwargs["text"], "Here is the result.")
        self.assertEqual(client.chat_update.await_args.kwargs["ts"], "progress-1")
        say.assert_not_awaited()

    async def test_native_status_uses_compatible_fallback_when_sessions_unavailable(self):
        transport = object.__new__(SlackTransport)
        client = AsyncMock()
        client.api_call.side_effect = SlackApiError(
            "not available", {"error": "feature_disabled"}
        )
        transport._app = SimpleNamespace(client=client)
        await transport._conversation_status("C123", "1", "processing")
        client.assistant_threads_setStatus.assert_awaited_with(
            channel_id="C123",
            thread_ts="1",
            status="is working…",
        )
        await transport._conversation_status("C123", "1", "active")
        client.assistant_threads_setStatus.assert_awaited_with(
            channel_id="C123",
            thread_ts="1",
            status="",
        )

    async def test_progress_update_failure_does_not_interrupt_the_tool(self):
        search = AsyncMock(return_value=[{"title": "Evidence"}])
        tools = RequestTools(
            runtime=SimpleNamespace(),
            request={"text": "research Reacher"},
            references=[],
            research=SimpleNamespace(search=search),
            progress=AsyncMock(side_effect=RuntimeError("Slack unavailable")),
        )
        with self.assertLogs("notion_db_updater.conversation", level="ERROR"):
            result = await tools.search("Reacher")
        self.assertEqual(result, [{"title": "Evidence"}])
        search.assert_awaited_once_with("Reacher")

    async def test_conversation_status_cleanup_and_approval_wait(self) -> None:
        for outcome, final_status in (
            ("Approve this exact action list (expires in 30 minutes):", "suspended"),
            (RuntimeError("Planner failed"), "active"),
            (asyncio.CancelledError(), "active"),
        ):
            with self.subTest(outcome=outcome):
                transport = object.__new__(SlackTransport)
                transport._owner = "U123"
                client = AsyncMock()
                transport._app = SimpleNamespace(client=client)

                async def handle(event, team, client=client, outcome=outcome, **kwargs):
                    self.assertEqual(
                        client.api_call.await_args.kwargs["json"],
                        {
                            "channel_id": "C123",
                            "thread_ts": "1",
                            "status": "processing",
                        },
                    )
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome

                transport._runtime = SimpleNamespace(
                    conversation=SimpleNamespace(handle=handle)
                )
                event = {
                    "user": "U123",
                    "channel": "C123",
                    "ts": "2",
                    "thread_ts": "1",
                    "text": "add Reacher",
                }
                if isinstance(outcome, BaseException):
                    with self.assertRaises(type(outcome)):
                        await transport.handle_conversation(event, AsyncMock(), "T123")
                else:
                    await transport.handle_conversation(event, AsyncMock(), "T123")
                self.assertEqual(
                    client.api_call.await_args.kwargs["json"]["status"], final_status
                )

    async def test_status_failure_does_not_block_reply_or_allow_other_users(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._owner = "U123"
        client = AsyncMock()
        client.api_call.side_effect = RuntimeError("Status unavailable")
        transport._app = SimpleNamespace(client=client)
        conversation = SimpleNamespace(handle=AsyncMock(return_value="Done."))
        transport._runtime = SimpleNamespace(conversation=conversation)
        event = {"user": "U123", "channel": "C123", "ts": "1", "text": "add Reacher"}
        say = AsyncMock()
        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport.handle_conversation(event, say, "T123")
        say.assert_awaited_once_with(text="Done.", thread_ts="1")
        client.api_call.reset_mock()
        conversation.handle.reset_mock()
        await transport.handle_conversation(event | {"user": "OTHER"}, say, "T123")
        client.api_call.assert_not_awaited()
        conversation.handle.assert_not_awaited()

    async def test_mention_routes_free_form_request_to_conversation(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._tasks = set()
        conversation = SimpleNamespace(handle=AsyncMock(return_value="Sweep started."))
        transport._runtime = SimpleNamespace(conversation=conversation)
        transport._owner = "U123"
        transport._app = SimpleNamespace(client=AsyncMock())
        say = AsyncMock()
        event = {
            "text": "<@U0BOT> run enrichment for pending Entries",
            "channel": "C123",
            "team": "T123",
            "user": "U123",
            "ts": "171234.567",
        }

        await transport._handle_mention(
            event,
            say,
            "T123",
        )
        await asyncio.gather(*transport._tasks)

        conversation.handle.assert_awaited_once_with(event, "T123", progress=ANY)
        say.assert_awaited_once_with(text="Sweep started.", thread_ts="171234.567")
        self.assertEqual(
            [
                call.kwargs["json"]["status"]
                for call in transport._app.client.api_call.await_args_list
            ],
            ["processing", "active"],
        )

    async def test_mention_without_conversation_reports_configuration_error(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._tasks = set()
        transport._runtime = SimpleNamespace(reconcile=AsyncMock())
        say = AsyncMock()

        await transport._handle_mention(
            {"text": "<@U0BOT> add Dune", "channel": "C123", "user": "U123"},
            say,
        )

        say.assert_awaited_once_with(
            "The conversational Watchlist assistant is not configured. "
            "Set OPENAI_AGENT_MODEL to enable it."
        )
        transport._runtime.reconcile.assert_not_awaited()
        self.assertFalse(transport._tasks)

    async def test_add_streams_real_node_progress_and_checkpoints_thread_ts(self) -> None:
        runtime = object.__new__(Runtime)
        graph = _FakeGraph()
        runtime._graph = graph
        runtime._notion = _FakeNotion()
        runtime._settings = SimpleNamespace(GRAPH_MAX_CONCURRENCY=0)
        runtime._inflight = set()
        runtime._identity_lock = asyncio.Lock()
        runtime._page_locks = {}
        runtime._notifier = None
        progress = AsyncMock()

        outcome = await runtime.create_and_enrich(
            "Dune",
            channel="C123",
            user="U123",
            thread_ts="171234.567",
            progress=progress,
        )

        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.title, "Dune")
        self.assertEqual(graph.initial_state["notify_thread_ts"], "171234.567")
        self.assertNotIn("notify_message_ts", graph.initial_state)
        self.assertEqual(
            [call.args[0] for call in progress.await_args_list],
            [
                "notion.creation.complete",
                "database.read.complete",
                "omdb.search.complete",
                "confidence.assessment.complete",
            ],
        )
        self.assertNotIn("page-1", runtime._inflight)

    async def test_cli_and_resolved_add_preserve_durable_pause_and_release_guard(self):
        payload = {"title": "Dune", "candidates": []}
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                runtime = object.__new__(Runtime)
                runtime._settings = SimpleNamespace(GRAPH_MAX_CONCURRENCY=2)
                runtime._identity_lock = asyncio.Lock()
                runtime._page_locks = {}
                runtime._inflight = {"page-1"} if resolved else set()
                runtime._notifier = AsyncMock()
                runtime._notion = SimpleNamespace(
                    create_entry=AsyncMock(return_value=_entry()), update_entry=AsyncMock()
                )
                graph = _FakeGraph()

                async def paused(initial_state, *, config, stream_mode):
                    self.assertEqual(config["max_concurrency"], 2)
                    self.assertEqual(config["configurable"]["thread_id"], "page-1")
                    yield {"__interrupt__": [SimpleNamespace(value=payload)]}

                graph.astream = paused
                runtime._graph = graph
                progress = AsyncMock()
                if resolved:
                    result = await runtime._enrich_resolved_entry(
                        _entry(), "tt1160419", "add Dune", "C123", "U123", "1"
                    )
                    self.assertEqual(result, {"status": "awaiting_input", "title": "Dune"})
                else:
                    result = await runtime.create_and_enrich("Dune", progress=progress)
                    self.assertEqual(result.status, "awaiting_input")
                    self.assertEqual(result.title, "Dune")
                    self.assertIsNone(result.year)
                    self.assertEqual(progress.await_args.args, ("human_input.required",))
                runtime._notion.update_entry.assert_awaited_once_with(
                    "page-1", enrichment_properties(status="awaiting_input")
                )
                runtime._notifier.assert_awaited_once_with("page-1", payload, None, None)
                self.assertFalse(runtime._inflight)
                self.assertFalse(runtime._page_lock("page-1").locked())

    async def test_completion_replies_in_original_message_thread(self) -> None:
        transport = object.__new__(SlackTransport)
        client = SimpleNamespace(chat_update=AsyncMock(), chat_postMessage=AsyncMock())
        transport._app = SimpleNamespace(client=client)

        await transport.post_completion("C123", "✅ Added Dune", "171234.567")

        client.chat_postMessage.assert_awaited_once_with(
            channel="C123",
            text="✅ Added Dune",
            thread_ts="171234.567",
            unfurl_links=False,
            unfurl_media=False,
        )
        client.chat_update.assert_not_awaited()

    async def test_completion_posts_to_channel_when_thread_timestamp_is_missing(self) -> None:
        transport = object.__new__(SlackTransport)
        client = SimpleNamespace(chat_update=AsyncMock(), chat_postMessage=AsyncMock())
        transport._app = SimpleNamespace(client=client)

        await transport.post_completion("C123", "✅ Added Dune")

        client.chat_postMessage.assert_awaited_once_with(
            channel="C123",
            text="✅ Added Dune",
            unfurl_links=False,
            unfurl_media=False,
        )
        client.chat_update.assert_not_awaited()

    async def test_completion_falls_back_to_channel_when_thread_reply_fails(self) -> None:
        transport = object.__new__(SlackTransport)
        client = SimpleNamespace(
            chat_update=AsyncMock(),
            chat_postMessage=AsyncMock(
                side_effect=[RuntimeError("thread_not_found"), {"ok": True}]
            ),
        )
        transport._app = SimpleNamespace(client=client)

        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport.post_completion("C123", "✅ Added Dune", "171234.567")

        self.assertEqual(
            client.chat_postMessage.await_args_list,
            [
                unittest.mock.call(
                    channel="C123",
                    text="✅ Added Dune",
                    thread_ts="171234.567",
                    unfurl_links=False,
                    unfurl_media=False,
                ),
                unittest.mock.call(
                    channel="C123",
                    text="✅ Added Dune",
                    unfurl_links=False,
                    unfurl_media=False,
                ),
            ],
        )

    async def test_slack_origin_pause_passes_checkpointed_picker_context(self) -> None:
        runtime = object.__new__(Runtime)
        runtime._notion = SimpleNamespace(update_entry=AsyncMock())
        runtime._notifier = AsyncMock()
        payload = {"title": "Dune", "candidates": []}

        paused = await runtime._handle_pause(
            _entry(),
            {
                "__interrupt__": [SimpleNamespace(value=payload)],
                "origin": "slack",
                "notify_channel": "C123",
                "notify_thread_ts": "171234.567",
            },
        )

        self.assertTrue(paused)
        runtime._notifier.assert_awaited_once_with("page-1", payload, "C123", "171234.567")

    async def test_reconcile_pause_keeps_configured_top_level_picker_fallback(self) -> None:
        runtime = object.__new__(Runtime)
        runtime._notion = SimpleNamespace(update_entry=AsyncMock())
        runtime._notifier = AsyncMock()
        payload = {"title": "Dune", "candidates": []}

        await runtime._handle_pause(
            _entry(), {"__interrupt__": [SimpleNamespace(value=payload)]}
        )

        runtime._notifier.assert_awaited_once_with("page-1", payload, None, None)

    async def test_picker_uses_originating_channel_and_thread(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._channel = "CFALLBACK"
        client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ok": True}))
        transport._app = SimpleNamespace(client=client)

        await transport.post_picker(
            "page-1",
            {"title": "Dune", "candidates": []},
            "C123",
            "171234.567",
        )

        kwargs = client.chat_postMessage.await_args.kwargs
        self.assertEqual(kwargs["channel"], "C123")
        self.assertEqual(kwargs["thread_ts"], "171234.567")

    async def test_reconcile_picker_is_top_level_in_configured_channel(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._channel = "CFALLBACK"
        client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ok": True}))
        transport._app = SimpleNamespace(client=client)

        await transport.post_picker("page-1", {"title": "Dune", "candidates": []})

        kwargs = client.chat_postMessage.await_args.kwargs
        self.assertEqual(kwargs["channel"], "CFALLBACK")
        self.assertNotIn("thread_ts", kwargs)


if __name__ == "__main__":
    unittest.main()

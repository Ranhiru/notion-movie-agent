from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

from notion_db_updater.app import Runtime
from notion_db_updater.models import Entry
from notion_db_updater.slack import LOADING_MESSAGES, SlackTransport, parse_mention_command


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


class SlackMentionParsingTests(unittest.TestCase):
    def test_parses_add_title_after_slack_mention(self) -> None:
        self.assertEqual(
            parse_mention_command("<@U0BOT> add Dune: Part Two"), ("add", "Dune: Part Two")
        )

    def test_command_is_case_insensitive_and_whitespace_is_trimmed(self) -> None:
        self.assertEqual(parse_mention_command("  <@U0BOT>   ADD   Dune  "), ("add", "Dune"))

    def test_missing_argument_is_preserved_for_usage_handling(self) -> None:
        self.assertEqual(parse_mention_command("<@U0BOT> add"), ("add", ""))

    def test_rejects_text_without_a_leading_slack_mention(self) -> None:
        self.assertIsNone(parse_mention_command("add Dune <@U0BOT>"))


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
    async def test_mention_add_schedules_tracked_background_flow(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._tasks = set()
        started = asyncio.Event()
        release = asyncio.Event()
        received: list[tuple[str, str, str | None, str | None, str, object]] = []
        set_status = AsyncMock()

        async def add_flow(
            title: str,
            channel: str,
            team_id: str | None,
            user: str | None,
            thread_ts: str,
            status,
        ) -> None:
            received.append((title, channel, team_id, user, thread_ts, status))
            started.set()
            await release.wait()

        transport._add_flow = add_flow
        transport._runtime = SimpleNamespace(reconcile=AsyncMock())
        say = AsyncMock()
        logger = Mock()

        await transport._handle_mention(
            {
                "text": "<@U0BOT> add Dune",
                "channel": "C123",
                "team": "T123",
                "user": "U123",
                "ts": "171234.567",
            },
            say,
            logger,
            set_status,
        )
        await started.wait()

        self.assertEqual(len(transport._tasks), 1)
        self.assertEqual(
            received, [("Dune", "C123", "T123", "U123", "171234.567", set_status)]
        )
        say.assert_not_awaited()
        logger.info.assert_called_once_with(
            "slack: mention add %r from %s in %s", "Dune", "U123", "C123"
        )

        task = next(iter(transport._tasks))
        release.set()
        await task

    async def test_mention_add_without_title_replies_with_usage(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._tasks = set()
        transport._runtime = SimpleNamespace(reconcile=AsyncMock())
        say = AsyncMock()

        await transport._handle_mention(
            {
                "text": "<@U0BOT> add",
                "channel": "C123",
                "user": "U123",
                "ts": "171234.567",
            },
            say,
            Mock(),
        )

        say.assert_awaited_once_with(
            "Usage: `@NotionMovieAgent add <title>` — e.g. `@NotionMovieAgent add Dune`"
        )
        self.assertFalse(transport._tasks)

    async def test_mention_add_without_channel_replies_with_error(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._tasks = set()
        transport._runtime = SimpleNamespace(reconcile=AsyncMock())
        say = AsyncMock()

        await transport._handle_mention(
            {"text": "<@U0BOT> add Dune", "user": "U123"}, say, Mock()
        )

        say.assert_awaited_once_with("Couldn't tell which channel to reply in — try again.")
        self.assertFalse(transport._tasks)

    async def test_mention_run_retains_reconcile_behavior(self) -> None:
        transport = object.__new__(SlackTransport)
        runtime = SimpleNamespace(reconcile=AsyncMock(return_value="1 enriched"))
        transport._runtime = runtime
        say = AsyncMock()

        await transport._handle_mention(
            {"text": "<@U0BOT> run", "channel": "C123", "user": "U123"}, say, Mock()
        )

        runtime.reconcile.assert_awaited_once_with()
        self.assertEqual(
            [call.args[0] for call in say.await_args_list],
            ["Running a reconcile sweep…", "reconcile: 1 enriched"],
        )

    async def test_unknown_mention_command_replies_with_help(self) -> None:
        transport = object.__new__(SlackTransport)
        transport._runtime = SimpleNamespace(reconcile=AsyncMock())
        say = AsyncMock()

        await transport._handle_mention(
            {"text": "<@U0BOT> dance", "channel": "C123", "user": "U123"}, say, Mock()
        )

        say.assert_awaited_once_with("Mention me with `add <title>` or `run`.")

    async def test_add_streams_real_node_progress_and_checkpoints_thread_ts(self) -> None:
        runtime = object.__new__(Runtime)
        graph = _FakeGraph()
        runtime._graph = graph
        runtime._notion = _FakeNotion()
        runtime._settings = SimpleNamespace(GRAPH_MAX_CONCURRENCY=0)
        runtime._inflight = set()
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

    @staticmethod
    def _stream_client(**overrides) -> SimpleNamespace:
        values = {
            "chat_startStream": AsyncMock(return_value={"ok": True, "ts": "stream-1"}),
            "chat_appendStream": AsyncMock(return_value={"ok": True}),
            "chat_stopStream": AsyncMock(return_value={"ok": True}),
            "chat_postMessage": AsyncMock(return_value={"ok": True}),
            "chat_update": AsyncMock(return_value={"ok": True}),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    async def test_add_starts_grouped_plan_with_loading_rotation_and_task_chunks(self) -> None:
        transport = object.__new__(SlackTransport)
        client = self._stream_client()
        set_status = AsyncMock()

        async def create_and_enrich(*args, progress, **kwargs) -> None:
            for event_id in (
                "notion.creation.complete",
                "database.read.complete",
                "omdb.search.complete",
                "omdb.details.complete",
                "rotten_tomatoes.complete",
                "sources.compare.complete",
                "match.verification.complete",
                "confidence.assessment.complete",
                "notion.update.complete",
            ):
                await progress(event_id)

        runtime = SimpleNamespace(
            find_duplicate=AsyncMock(return_value=None),
            create_and_enrich=AsyncMock(side_effect=create_and_enrich),
        )
        transport._app = SimpleNamespace(client=client)
        transport._runtime = runtime

        await transport._add_flow("Dune", "C123", "T123", "U123", "171234.567", set_status)

        runtime.create_and_enrich.assert_awaited_once_with(
            "Dune",
            channel="C123",
            user="U123",
            thread_ts="171234.567",
            progress=ANY,
        )
        client.chat_startStream.assert_awaited_once_with(
            channel="C123",
            thread_ts="171234.567",
            recipient_team_id="T123",
            recipient_user_id="U123",
            task_display_mode="plan",
        )
        self.assertEqual(len(LOADING_MESSAGES), 10)
        self.assertEqual(len(set(LOADING_MESSAGES)), 10)
        set_status.assert_awaited_once_with(
            status="Adding Dune to your watchlist…",
            loading_messages=LOADING_MESSAGES,
        )
        chunks = [
            chunk
            for call in client.chat_appendStream.await_args_list
            for chunk in call.kwargs["chunks"]
        ]
        self.assertTrue(chunks)
        self.assertTrue(all(chunk["type"] == "task_update" for chunk in chunks))
        self.assertTrue(all(len(chunk["title"]) < 256 for chunk in chunks))
        self.assertEqual(
            {chunk["status"] for chunk in chunks},
            {"pending", "in_progress", "complete"},
        )
        self.assertEqual(
            {chunk["id"] for chunk in chunks},
            {
                "deduplication",
                "notion_creation",
                "database_search",
                "title_matching",
                "disambiguation",
                "omdb_details",
                "rotten_tomatoes",
                "source_comparison",
                "verification",
                "confidence",
                "notion_update",
            },
        )
        for task_id in {chunk["id"] for chunk in chunks}:
            self.assertEqual(
                [chunk["status"] for chunk in chunks if chunk["id"] == task_id],
                ["pending", "in_progress", "complete"],
            )
        client.chat_stopStream.assert_awaited_once_with(channel="C123", ts="stream-1")
        client.chat_postMessage.assert_not_awaited()
        client.chat_update.assert_not_awaited()

    async def test_slack_stream_and_status_failures_do_not_gate_enrichment(self) -> None:
        transport = object.__new__(SlackTransport)
        client = self._stream_client(
            chat_startStream=AsyncMock(side_effect=RuntimeError("Slack unavailable"))
        )
        runtime = SimpleNamespace(
            find_duplicate=AsyncMock(return_value=None),
            create_and_enrich=AsyncMock(),
        )
        transport._app = SimpleNamespace(client=client)
        transport._runtime = runtime
        set_status = AsyncMock(side_effect=RuntimeError("Slack unavailable"))

        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport._add_flow("Dune", "C123", "T123", "U123", "171234.567", set_status)

        runtime.create_and_enrich.assert_awaited_once_with(
            "Dune",
            channel="C123",
            user="U123",
            thread_ts="171234.567",
            progress=ANY,
        )
        client.chat_appendStream.assert_not_awaited()
        client.chat_stopStream.assert_not_awaited()

    async def test_append_failure_does_not_gate_enrichment_and_open_stream_stops(self) -> None:
        transport = object.__new__(SlackTransport)
        client = self._stream_client(
            chat_appendStream=AsyncMock(side_effect=RuntimeError("Slack unavailable"))
        )
        runtime = SimpleNamespace(
            find_duplicate=AsyncMock(return_value=None),
            create_and_enrich=AsyncMock(),
        )
        transport._app = SimpleNamespace(client=client)
        transport._runtime = runtime

        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport._add_flow(
                "Dune", "C123", "T123", "U123", "171234.567", AsyncMock()
            )

        runtime.create_and_enrich.assert_awaited_once()
        client.chat_stopStream.assert_awaited_once_with(channel="C123", ts="stream-1")

    async def test_duplicate_result_is_a_thread_reply_and_stops_stream(self) -> None:
        transport = object.__new__(SlackTransport)
        client = self._stream_client()
        transport._app = SimpleNamespace(client=client)
        transport._runtime = SimpleNamespace(find_duplicate=AsyncMock(return_value=_entry()))

        await transport._add_flow("Dune", "C123", "T123", "U123", "171234.567", AsyncMock())

        client.chat_postMessage.assert_awaited_once_with(
            channel="C123",
            text="*Dune* is already on the watchlist (status: *pending*) — nothing added.",
            thread_ts="171234.567",
        )
        client.chat_stopStream.assert_awaited_once_with(channel="C123", ts="stream-1")

    async def test_graph_failure_marks_active_task_error_and_stops_stream(self) -> None:
        transport = object.__new__(SlackTransport)
        client = self._stream_client()
        transport._app = SimpleNamespace(client=client)
        transport._runtime = SimpleNamespace(
            find_duplicate=AsyncMock(return_value=None),
            create_and_enrich=AsyncMock(side_effect=RuntimeError("graph failed")),
        )

        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport._add_flow(
                "Dune", "C123", "T123", "U123", "171234.567", AsyncMock()
            )

        error_chunks = [
            chunk
            for call in client.chat_appendStream.await_args_list
            for chunk in call.kwargs["chunks"]
            if chunk["status"] == "error"
        ]
        self.assertEqual([chunk["id"] for chunk in error_chunks], ["notion_creation"])
        client.chat_stopStream.assert_awaited_once_with(channel="C123", ts="stream-1")
        client.chat_postMessage.assert_awaited_once_with(
            channel="C123",
            text="Couldn't add *Dune* — something went wrong. Try again?",
            thread_ts="171234.567",
        )

    async def test_hitl_pause_marks_disambiguation_and_stops_stream(self) -> None:
        transport = object.__new__(SlackTransport)
        client = self._stream_client()

        async def pause(*args, progress, **kwargs) -> None:
            await progress("notion.creation.complete")
            await progress("database.read.complete")
            await progress("omdb.search.complete")
            await progress("human_input.required")

        transport._app = SimpleNamespace(client=client)
        transport._runtime = SimpleNamespace(
            find_duplicate=AsyncMock(return_value=None),
            create_and_enrich=AsyncMock(side_effect=pause),
        )

        await transport._add_flow("Dune", "C123", "T123", "U123", "171234.567", AsyncMock())

        error_chunks = [
            chunk
            for call in client.chat_appendStream.await_args_list
            for chunk in call.kwargs["chunks"]
            if chunk["status"] == "error"
        ]
        self.assertEqual([chunk["id"] for chunk in error_chunks], ["disambiguation"])
        client.chat_stopStream.assert_awaited_once_with(channel="C123", ts="stream-1")

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
        client = self._stream_client()
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
        client = self._stream_client()
        transport._app = SimpleNamespace(client=client)

        await transport.post_picker("page-1", {"title": "Dune", "candidates": []})

        kwargs = client.chat_postMessage.await_args.kwargs
        self.assertEqual(kwargs["channel"], "CFALLBACK")
        self.assertNotIn("thread_ts", kwargs)


if __name__ == "__main__":
    unittest.main()

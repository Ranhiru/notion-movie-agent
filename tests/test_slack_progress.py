from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

from notion_db_updater.app import Runtime
from notion_db_updater.models import Entry
from notion_db_updater.slack import SlackTransport, parse_mention_command


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
        received: list[tuple[str, str, str | None]] = []

        async def add_flow(title: str, channel: str, user: str | None) -> None:
            received.append((title, channel, user))
            started.set()
            await release.wait()

        transport._add_flow = add_flow
        transport._runtime = SimpleNamespace(reconcile=AsyncMock())
        say = AsyncMock()
        logger = Mock()

        await transport._handle_mention(
            {"text": "<@U0BOT> add Dune", "channel": "C123", "user": "U123"},
            say,
            logger,
        )
        await started.wait()

        self.assertEqual(len(transport._tasks), 1)
        self.assertEqual(received, [("Dune", "C123", "U123")])
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
            {"text": "<@U0BOT> add", "channel": "C123", "user": "U123"}, say, Mock()
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

    async def test_add_streams_real_node_progress_and_checkpoints_message_ts(self) -> None:
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
            message_ts="171234.567",
            progress=progress,
        )

        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.title, "Dune")
        self.assertEqual(graph.initial_state["notify_message_ts"], "171234.567")
        self.assertEqual(
            [call.args[0] for call in progress.await_args_list],
            [
                "✅ Notion entry created — starting enrichment…",
                "🔎 Searching movie databases…",
                "🎬 Checking the best title match…",
                "📝 Updating Notion…",
            ],
        )
        self.assertNotIn("page-1", runtime._inflight)

    async def test_completion_edits_existing_progress_message(self) -> None:
        transport = object.__new__(SlackTransport)
        client = SimpleNamespace(chat_update=AsyncMock(), chat_postMessage=AsyncMock())
        transport._app = SimpleNamespace(client=client)

        await transport.post_completion("C123", "✅ Added Dune", "171234.567")

        client.chat_update.assert_awaited_once_with(
            channel="C123",
            ts="171234.567",
            text="✅ Added Dune",
            unfurl_links=False,
            unfurl_media=False,
        )
        client.chat_postMessage.assert_not_awaited()

    async def test_completion_posts_when_no_progress_message_exists(self) -> None:
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

    async def test_completion_falls_back_to_post_when_progress_message_is_gone(self) -> None:
        transport = object.__new__(SlackTransport)
        client = SimpleNamespace(
            chat_update=AsyncMock(side_effect=RuntimeError("message_not_found")),
            chat_postMessage=AsyncMock(),
        )
        transport._app = SimpleNamespace(client=client)

        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport.post_completion("C123", "✅ Added Dune", "171234.567")

        client.chat_postMessage.assert_awaited_once_with(
            channel="C123",
            text="✅ Added Dune",
            unfurl_links=False,
            unfurl_media=False,
        )

    async def test_initial_progress_failure_does_not_gate_enrichment(self) -> None:
        transport = object.__new__(SlackTransport)
        client = SimpleNamespace(
            chat_postMessage=AsyncMock(side_effect=RuntimeError("Slack unavailable")),
            chat_update=AsyncMock(),
        )
        runtime = SimpleNamespace(
            find_duplicate=AsyncMock(return_value=None),
            create_and_enrich=AsyncMock(),
        )
        transport._app = SimpleNamespace(client=client)
        transport._runtime = runtime

        with self.assertLogs("notion_db_updater.slack", level="ERROR"):
            await transport._add_flow("Dune", "C123", "U123")

        runtime.create_and_enrich.assert_awaited_once_with(
            "Dune",
            channel="C123",
            user="U123",
            message_ts=None,
            progress=ANY,
        )


if __name__ == "__main__":
    unittest.main()

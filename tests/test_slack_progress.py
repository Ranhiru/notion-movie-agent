from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

from notion_db_updater.app import Runtime
from notion_db_updater.models import Entry
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

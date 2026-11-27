from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from notion_db_updater.agent import build_planner
from notion_db_updater.conversation import Action, Conversation, RequestTools
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
        self.conversation = Conversation(
            self.runtime,
            str(Path(self.temp.name) / "conversation.sqlite"),
            self._planner(
                tool_call("resolve_title", {"query": "Lone Survivor"}, "resolve-1"),
                tool_call(
                    "add_entry",
                    {"imdb_id": "tt1091191"},
                    "add-1",
                ),
                reply_call("Added Lone Survivor.", [], "reply-1"),
            ),
            research=self.runtime.research,
        )
        self.transport = object.__new__(SlackTransport)
        self.transport._runtime = cast(Any, SimpleNamespace(conversation=self.conversation))
        self.transport._tasks = set()
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
        if say.await_args is not None:
            return say.await_args.kwargs["text"]
        update = self.transport._app.client.chat_update.await_args
        if update is not None:
            return update.kwargs["text"]
        raise AssertionError("Slack transport produced no reply")

    async def test_agent_resolves_and_stages_before_runtime_add(self) -> None:
        result = await self._send("Add Lone Survivor", ts="1")
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

    async def test_agent_clarification_reaches_slack_as_candidate_card(self) -> None:
        self.runtime.resolve_title = AsyncMock(
            return_value=[
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
        )
        self.conversation.planner = self._planner(
            tool_call("resolve_title", {"query": "Dark Matter"}, "resolve-dark"),
            tool_call(
                "Reply",
                {
                    "text": "Which Dark Matter did you mean?",
                    "disambiguations": ["Dark Matter"],
                },
                "reply-dark",
            ),
        )
        client = self.transport._app.client
        client.chat_postMessage.return_value = {"ts": "progress"}
        event = {
            "text": "<@BOT> Add Dark Matter",
            "channel": "C",
            "user": "owner",
            "ts": "dark",
            "team": "T",
        }
        await self.transport.handle_conversation(event, AsyncMock(), "T")
        blocks = client.chat_update.await_args.kwargs["blocks"]
        buttons = next(b for b in blocks if b["type"] == "actions")["elements"]
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(b["action_id"].startswith("conversation:pick:") for b in buttons))
        self.assertEqual(self.runtime.writes, [])

    async def test_six_film_resolution_loop_returns_grounded_slack_choices(self) -> None:
        titles = ["Anora", "Nomandland", "Parasite", "The Shape of Water", "Moonlight", "Argo"]
        identities = {
            title: {"title": title, "year": 2024, "imdb_id": f"tt{index:07}"}
            for index, title in enumerate(titles, 1)
        }
        identities["Nomadland"] = identities.pop("Nomandland") | {"title": "Nomadland"}

        async def resolve(query: str, media_type: str | None = None) -> list[dict]:
            return [identities[query]] if query in identities else []

        self.runtime.resolve_title = AsyncMock(side_effect=resolve)
        self.conversation.planner = self._planner(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "resolve_title", "args": {"query": title}, "id": f"r-{i}"}
                    for i, title in enumerate(titles)
                ],
            ),
            tool_call(
                "resolve_title",
                {"query": "Nomadland", "requested_title": "Nomandland"},
                "correct-typo",
            ),
            tool_call(
                "resolve_title",
                {"query": "Anora movie 2024", "requested_title": "Anora"},
                "embellished",
            ),
            tool_call(
                "resolve_title",
                {"query": "Anora Sean Baker", "requested_title": "Anora"},
                "over-budget",
            ),
        )
        result = await self._send("add " + ", ".join(titles), ts="six", mention=True)
        event = {"channel": "C", "ts": "six"}
        choices = self.conversation.disambiguations(event, "T")
        self.assertEqual(len(choices), 6, result)
        self.assertEqual(choices[0]["candidates"], [identities["Anora"]])
        self.assertEqual(choices[1]["candidates"], [identities["Nomadland"]])
        self.assertEqual(self.runtime.resolve_title.await_count, 8)
        self.assertEqual(self.runtime.writes, [])

        # Cards and the complete plan survive restart and each subsequent choice.
        self.conversation = Conversation(
            self.runtime, self.conversation.db_path, self.conversation.planner
        )
        verified = {c["imdb_id"]: c for c in identities.values()}
        self.runtime.validate_identity = AsyncMock(
            side_effect=lambda identity: verified[identity]
        )
        self.runtime.add_resolved = AsyncMock(return_value={"status": "done"})
        for i in range(6):
            choice = self.conversation.disambiguations(event, "T")[0]
            remaining = list(identities.values())[i + 1 :]
            self.conversation.planner = self._planner(
                *[
                    tool_call("resolve_title", {"query": c["title"]}, f"remaining-{j}")
                    for j, c in enumerate(remaining)
                ],
                tool_call(
                    "add_entry", {"imdb_id": choice["candidates"][0]["imdb_id"]}, "chosen"
                ),
                tool_call(
                    "Reply",
                    {
                        "text": "Choose the remaining titles.",
                        "disambiguations": [c["title"] for c in remaining],
                    },
                    "remaining-choices",
                ),
            )
            result = await self.conversation.choose_candidate(
                choice["request_key"],
                choice["token"],
                choice["candidates"][0]["imdb_id"],
                event | {"thread_ts": "six"},
                "T",
            )
            event = {"channel": "C", "ts": f"choice:{choice['token']}"}
            self.runtime.add_resolved.assert_not_awaited()
        self.assertIn("6. add", result)
        token = re.search(r"`approve ([0-9a-f]{16})`", result)[1]
        await self.conversation.handle(
            event | {"ts": "approve-six", "thread_ts": "six", "text": f"approve {token}"},
            "T",
        )
        self.assertEqual(self.runtime.add_resolved.await_count, 6)

    async def test_one_title_search_limit_does_not_disambiguate_the_complete_request(
        self,
    ) -> None:
        titles = ["Anora", "Nomandland", "Parasite", "The Shape of Water", "Moonlight", "Argo"]
        identities = {
            title: {"title": title, "year": 2024, "imdb_id": f"tt{index:07}"}
            for index, title in enumerate(titles, 1)
        }

        async def resolve(query: str, media_type: str | None = None) -> list[dict]:
            return [identities[query]] if query in identities and query != "Nomandland" else []

        self.runtime.resolve_title = AsyncMock(side_effect=resolve)
        verified = {c["imdb_id"]: c for c in identities.values()}
        self.runtime.validate_identity = AsyncMock(
            side_effect=lambda identity: verified[identity]
        )
        self.runtime.add_resolved = AsyncMock(return_value={"status": "done"})
        self.conversation.planner = self._planner(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "resolve_title", "args": {"query": title}, "id": f"r-{i}"}
                    for i, title in enumerate(titles)
                ],
            ),
            tool_call(
                "resolve_title",
                {"query": "Nomadland", "requested_title": "Nomandland"},
                "correction",
            ),
            tool_call(
                "resolve_title",
                {"query": "Nomadland", "media_type": "movie", "requested_title": "Nomandland"},
                "over-budget",
            ),
            *[
                tool_call("add_entry", {"imdb_id": identities[title]["imdb_id"]}, f"add-{i}")
                for i, title in enumerate(titles)
                if title != "Nomandland"
            ],
            tool_call(
                "Reply",
                {
                    "text": "Which film did you mean by Nomandland?",
                    "disambiguations": ["Nomandland"],
                },
                "reply",
            ),
        )
        result = await self._send("add " + ", ".join(titles), ts="one-limited", mention=True)
        choices = self.conversation.disambiguations({"channel": "C", "ts": "one-limited"}, "T")
        self.assertEqual([choice["title"] for choice in choices], ["Nomandland"], result)
        self.assertEqual(self.runtime.resolve_title.await_count, 7)
        self.assertEqual(self.runtime.writes, [])
        saved = self.conversation._load("request:T:C:one-limited")
        self.assertEqual(len(saved["actions"]), 5)
        choice = choices[0]
        self.conversation.planner = self._planner(
            tool_call("add_entry", {"imdb_id": identities["Nomandland"]["imdb_id"]}, "chosen"),
            reply_call("The complete request is ready.", [], "ready"),
        )
        event = {"channel": "C", "thread_ts": "one-limited"}
        result = await self.conversation.choose_candidate(
            choice["request_key"],
            choice["token"],
            identities["Nomandland"]["imdb_id"],
            event,
            "T",
            manual=True,
        )
        self.assertIn("6. add", result)
        self.runtime.add_resolved.assert_not_awaited()
        token = re.search(r"`approve ([0-9a-f]{16})`", result)[1]
        await self.conversation.handle(
            event | {"ts": "approve-six", "text": f"approve {token}"}, "T"
        )
        self.assertEqual(self.runtime.add_resolved.await_count, 6)

    async def test_repeated_parallel_misses_stop_with_manual_slack_input(self) -> None:
        self.runtime.resolve_title = AsyncMock(return_value=[])
        self.transport._app.client.chat_postMessage.return_value = {"ts": "progress"}
        self.conversation.planner = self._planner(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "resolve_title", "args": {"query": "Missing"}, "id": f"r-{i}"}
                    for i in range(4)
                ],
            )
        )
        result = await self._send("Add Missing", ts="missing", mention=True)
        self.assertIn("Missing", result)
        saved = self.conversation._load("request:T:C:missing")
        self.assertIn("unfinished", saved["response"])
        self.runtime.resolve_title.assert_awaited_once()
        blocks = self.transport._app.client.chat_update.await_args.kwargs["blocks"]
        self.assertTrue(any(b["type"] == "input" for b in blocks))
        self.assertFalse(any(b["type"] == "actions" for b in blocks))
        self.assertEqual(self.runtime.writes, [])

    async def test_planner_limit_without_unresolved_titles_keeps_staged_actions(self) -> None:
        self.conversation.planner = self._planner(
            tool_call("add_entry", {"imdb_id": "tt1091191"}, "staged"),
            *[tool_call("list_watchlist", {}, f"repeat-{i}") for i in range(60)],
        )
        result = await self._send("Add Lone Survivor", ts="unfinished", mention=True)
        self.assertIn("unfinished", result)
        saved = self.conversation._load("request:T:C:unfinished")
        self.assertEqual(saved["status"], "clarifying")
        self.assertEqual(len(saved["actions"]), 1)
        self.assertEqual(self.runtime.writes, [])

    async def test_25_entry_plan_fits_with_sequential_resolve_and_stage_calls(self) -> None:
        identities = {
            f"Movie {i}": {"title": f"Movie {i}", "imdb_id": f"tt{i:07}", "year": 2024}
            for i in range(25)
        }
        verified = {c["imdb_id"]: c for c in identities.values()}
        self.runtime.resolve_title = AsyncMock(
            side_effect=lambda query, kind: [identities[query]]
        )
        self.runtime.validate_identity = AsyncMock(
            side_effect=lambda identity: verified[identity]
        )
        self.conversation.planner = self._planner(
            *[
                call
                for title, identity in identities.items()
                for call in (
                    tool_call("resolve_title", {"query": title}, f"resolve-{title}"),
                    tool_call("add_entry", {"imdb_id": identity["imdb_id"]}, f"add-{title}"),
                )
            ],
            reply_call("Ready for approval.", [], "final"),
        )
        result = await self._send("Add " + ", ".join(identities), ts="25", mention=True)
        self.assertIn("25. add", result)
        self.assertIn("Approve", result)
        self.assertEqual(self.runtime.resolve_title.await_count, 25)
        self.assertEqual(self.runtime.writes, [])

    async def test_unverified_imdb_identity_still_requires_clarification(self) -> None:
        self.conversation.planner = self._planner(
            tool_call("add_entry", {"imdb_id": "tt9999999"}, "add-invalid"),
            reply_call("Added a movie.", [], "reply-invalid"),
        )
        with self.assertLogs("notion_db_updater.conversation", level="ERROR"):
            result = await self._send("Add Lone Survivor", ts="invalid-id", mention=True)
        self.assertIn("clarification_required", result)
        self.assertEqual(self.runtime.writes, [])

    async def test_alias_can_resolve_with_a_query_not_quoted_in_the_request(self) -> None:
        identity = {
            "imdb_id": "tt1091191",
            "title": "The Lord of the Rings",
            "year": 2001,
            "media_type": "Movie",
        }

        async def resolve_title(query: str, media_type: str | None = None) -> list[dict]:
            self.assertEqual(query, "The Lord of the Rings")
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
            tool_call("resolve_title", {"query": "The Lord of the Rings"}, "resolve-alias"),
            tool_call(
                "add_entry",
                {"imdb_id": "tt1091191"},
                "add-alias",
            ),
            reply_call("Added The Lord of the Rings.", [], "reply-alias"),
        )
        result = await self._send("Add LOTR", ts="alias", mention=True)
        self.assertIn("The Lord of the Rings", result)
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


class ResolutionBudgetTests(unittest.IsolatedAsyncioTestCase):
    def context(self, *, max_titles: int = 25) -> RequestTools:
        return RequestTools(
            SimpleNamespace(resolve_title=AsyncMock(return_value=[])),
            {},
            [],
            max_titles=max_titles,
        )

    async def test_parallel_normalized_queries_and_empty_results_are_cached(self) -> None:
        context = self.context()
        results = await asyncio.gather(
            context.resolve_title("Nomandland", "movie"),
            context.resolve_title("  NOMANDLAND  ", "Movie"),
        )
        self.assertEqual(results, [[], []])
        context.runtime.resolve_title.assert_awaited_once_with("Nomandland", "movie")
        corrected = {"title": "Nomadland", "imdb_id": "tt9770150"}
        context.runtime.resolve_title.return_value = [corrected]
        self.assertEqual(
            await context.resolve_title("Nomadland", "movie", "Nomandland"), [corrected]
        )
        await context.resolve_title("Nomadland Frances McDormand", "movie", "Nomandland")
        self.assertFalse(context.resolution_limited)
        self.assertEqual(context.runtime.resolve_title.await_count, 2)
        self.assertEqual(context.candidates["Nomandland"], [corrected])
        self.assertEqual(
            await context.resolve_title("Nomadland", "movie", "Nomandland"), [corrected]
        )
        await context.resolve_title("Anora")
        self.assertEqual(context.runtime.resolve_title.await_count, 3)

    async def test_media_types_have_distinct_cached_results(self) -> None:
        context = self.context()
        await context.resolve_title("Dark Matter", "movie")
        await context.resolve_title("Dark Matter", "tv")
        await context.resolve_title("Dark Matter", "series")
        self.assertEqual(context.runtime.resolve_title.await_count, 2)
        self.assertFalse(context.resolution_limited)

    async def test_new_labels_cannot_bypass_request_search_ceiling(self) -> None:
        context = self.context(max_titles=2)
        for title in ("One", "Two"):
            await context.resolve_title(title)
            await context.resolve_title(title + " corrected", requested_title=title)
        await context.resolve_title("Three")
        self.assertEqual(context.runtime.resolve_title.await_count, 4)
        self.assertTrue(context.resolution_limited)
        self.assertIn("unfinished", context.stop_planning().text)

    async def test_model_ceiling_counts_cached_calls_and_resets_for_each_request(self) -> None:
        model = ScriptedToolModel(
            responses=[tool_call("resolve_title", {"query": "Missing"}, "repeat")]
        )
        planner = build_planner(model)
        for _ in range(2):
            context = self.context()
            reply = await planner([{"role": "user", "content": "Add Missing"}], context)
            self.assertEqual(context.model_calls, 55)
            self.assertEqual(reply.disambiguations, ["Missing"])
            self.assertTrue(context.planning_stopped)
            context.runtime.resolve_title.assert_awaited_once()

    async def test_fallback_retains_staged_add_and_unresolved_candidates(self) -> None:
        context = self.context()
        candidate = {"title": "Anora", "imdb_id": "tt28607951"}
        context.runtime.resolve_title.return_value = [candidate]
        await context.resolve_title("Anora")
        context.stage(Action(operation="add", imdb_id=candidate["imdb_id"]))
        context.runtime.resolve_title.return_value = []
        await context.resolve_title("Nomandland")
        reply = context.stop_planning()
        self.assertEqual(reply.disambiguations, ["Nomandland"])
        self.assertEqual(len(context.actions), 1)


if __name__ == "__main__":
    unittest.main()

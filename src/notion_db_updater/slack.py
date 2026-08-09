"""Slack Bolt (Socket Mode) transport — the app's only inbound path (ADR 0009 / 0010).

A single outbound WebSocket carries all inbound interaction, so there's no public HTTP endpoint
(works in local Docker). Three jobs:

1. **HITL disambiguation (ADR 0006):** when a reconcile run pauses at `interrupt()`, `Runtime`
   calls `post_picker` — a Block Kit prompt of the OMDb candidates is posted to
   `#notion-movie-db`. The button click resumes the paused graph with the chosen imdbID.
2. **Manual run:** an `@movie-bot run` app-mention triggers `reconcile()` (under the same
   single-flight lock as the cron; ADR 0001).
3. **`@movie-bot add <title>` (Phase 9 / ADR 0012):** an app mention originates an Entry from
   Slack. The handler schedules the dedupe / create / out-of-band enrich in a background task;
   progress is rendered as a streamed grouped task plan, while the completion ping is posted
   as a reply in the mention's thread by the graph's terminal `notify` node
   (via `post_completion`, bound into `Runtime`). A run may only reach `done`/`failed` much
   later — after a disambiguation click or the 6d auto-resolve.

`Runtime` and this transport are mutually referential — the sweep posts *to* Slack (via
`Runtime.set_notifier(post_picker)` + `Runtime.bind_completion_notifier(post_completion)`) and
Slack interactions call *back into* `Runtime.resume` / `.reconcile` / `.create_and_enrich`.
`_serve` wires all three, then runs the cron loop and the Socket Mode listener concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import Settings

if TYPE_CHECKING:
    from .app import Runtime

log = logging.getLogger(__name__)

# Block Kit / Slack limits: at most 5 candidates rendered (one section + one button each);
# button text is capped well under Slack's 75-char limit.
_MAX_CANDIDATES = 5
_BUTTON_LABEL_MAX = 70
_PICK_ACTION = re.compile(r"^pick:\d+$")  # one action_id per button: pick:0 … pick:4
# The manual-input escape hatch (6e): deliberately outside the `pick:\d+` regex so the
# candidate-button handler can never receive it. `block_id` carries the page_id (see below).
_MANUAL_ACTION = "manual:submit"
_MANUAL_BLOCK_PREFIX = "manual_input:"
_IMDB_ID = re.compile(r"tt\d+")  # matches a full imdb.com/title/tt… URL or a bare tt… id
# Slack renders a bot mention as `<@BOT_USER_ID>`. Commands must immediately follow the
# leading mention; the argument remains untouched apart from surrounding whitespace.
_MENTION_COMMAND = re.compile(
    r"^\s*<@[^>]+>(?:\s+(?P<command>\S+))?(?:\s+(?P<argument>.*?))?\s*$"
)

# IMDb title page for a given imdbID — rendered as a clickable link in the picker.
_IMDB_URL = "https://www.imdb.com/title/{}/"


def _fmt_year(year: str | None) -> str:
    """OMDb `Year` for display: a plain movie year ("2021") or a series range ("2013–2017").

    An open-ended range ("2013–", ongoing series) reads better as "2013–present".
    """
    y = (year or "").strip()
    if y.endswith(("-", "–", "—")):
        y = y[:-1].strip() + "–present"
    return y


def _button_label(c: dict) -> str:
    """Button text: "Title (year/range)", capped to Slack's limit with the year kept intact.

    If the combined text exceeds `_BUTTON_LABEL_MAX`, only the title is truncated — the year
    suffix always survives, so two same-titled candidates stay distinguishable at a glance.
    """
    title = c.get("title") or "?"
    year = _fmt_year(c.get("year"))
    suffix = f" ({year})" if year else ""
    if len(title) + len(suffix) > _BUTTON_LABEL_MAX:
        title = title[: _BUTTON_LABEL_MAX - len(suffix)]
    return f"{title}{suffix}"


def build_picker_blocks(page_id: str, payload: dict) -> list[dict]:
    """Render the disambiguation `interrupt()` payload into a Block Kit candidate picker.

    One `section` per candidate (title · year · type, poster as an image accessory when OMDb
    returned one), then one `actions` block of buttons whose `value` encodes `page_id` (the
    graph `thread_id`) + the chosen `imdbID` — all the action handler needs to resume the right
    run. Note: OMDb `?s=` search results carry no plot, so the per-candidate plot in the
    original mockup is omitted rather than paying N extra `?i=` detail calls just to render the
    prompt; title/year/type/poster is enough to disambiguate a watchlist title.

    Below the buttons is a manual-input escape hatch (6e): a `plain_text_input` for when the
    right title isn't among the ≤5 OMDb candidates (OMDb `?s=` never surfaced it). Pasting an
    IMDb link or bare `tt…` id resumes the *same* paused run — no graph change; the resume path
    already accepts an arbitrary imdbID. The `page_id` rides in the input block's `block_id`
    (input elements carry no per-action `value`), so the handler stays self-contained.

    A **candidate-less** payload is the Phase-6f not-found escalation (OMDb returned nothing
    even after title normalization): the header shifts to a "couldn't find" prompt and the
    candidate `actions` block is dropped (an empty `elements` array is invalid Block Kit),
    leaving the manual-input field as the sole control.
    """
    title = payload.get("title") or "(unknown title)"
    best_guess = payload.get("best_guess_imdb_id")
    candidates = (payload.get("candidates") or [])[:_MAX_CANDIDATES]

    header = (
        f"*Which _{title}_ did you mean?*"
        if candidates
        else f"*Couldn't find _{title}_ on OMDb* — paste the IMDb link or ID below."
    )
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for c in candidates:
        label = c.get("title") or "(untitled)"
        bits = f"*{label}*"
        year = _fmt_year(c.get("year"))
        if year:
            bits += f"  ({year})"
        if c.get("media_type"):
            bits += f" · {c['media_type']}"
        if c.get("imdb_id"):
            bits += f" · <{_IMDB_URL.format(c['imdb_id'])}|IMDb ↗>"
        if c.get("imdb_id") == best_guess:
            bits += "   _⭐ best guess_"
        section: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": bits}}
        poster = c.get("poster")
        if poster and poster.startswith("http"):
            section["accessory"] = {"type": "image", "image_url": poster, "alt_text": label}
        blocks.append(section)

    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": _button_label(c)},
            "value": json.dumps({"page_id": page_id, "imdb_id": c["imdb_id"]}),
            "action_id": f"pick:{i}",
        }
        for i, c in enumerate(candidates)
    ]
    # 6f: a not-found escalation has no candidates → skip the (invalid) empty actions block.
    if buttons:
        blocks.append({"type": "actions", "block_id": "pick_actions", "elements": buttons})
    blocks.append(
        {
            "type": "input",
            "dispatch_action": True,  # fire an action on Enter instead of a submit button
            "block_id": f"{_MANUAL_BLOCK_PREFIX}{page_id}",
            "label": {
                "type": "plain_text",
                "text": "None of the above? Paste the IMDb link or ID",
            },
            "element": {
                "type": "plain_text_input",
                "action_id": _MANUAL_ACTION,
                "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]},
                "placeholder": {
                    "type": "plain_text",
                    "text": "https://www.imdb.com/title/tt… or tt…",
                },
            },
        }
    )
    return blocks


def _section_blocks(text: str) -> list[dict]:
    """A one-section mrkdwn message (used to replace the picker as it resolves)."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _resolved_blocks(user: str | None, label: str, imdb_url: str, status: str) -> list[dict]:
    """The terminal ✅ message replacing the picker once a run resolves (button or manual)."""
    return _section_blocks(
        f"✅ <@{user}> picked *{label}*  ·  <{imdb_url}|IMDb ↗>\n"
        f"_Enrichment status: *{status}*_"
    )


def parse_mention_command(text: str) -> tuple[str, str] | None:
    """Parse Slack's ``<@BOT_ID> command argument`` app-mention text.

    Returns a case-folded command plus its trimmed argument. ``None`` means the event did not
    start with a Slack mention, which is malformed for this listener but useful to handle
    defensively in tests and synthetic payloads.
    """
    match = _MENTION_COMMAND.fullmatch(text)
    if match is None:
        return None
    return (match.group("command") or "").casefold(), (match.group("argument") or "").strip()


# Slack rotates these while the title-specific assistant status is visible. Keep this exact
# production-themed set stable: it is deliberate product copy, not transport-neutral runtime
# progress (and every item is comfortably below Slack's status limits).
LOADING_MESSAGES = [
    "Rewinding the tape for clues…",
    "Checking continuity with the script supervisor…",
    "Sending the title through casting…",
    "Asking the projectionist to focus…",
    "Searching the backlot for the right cut…",
    "Comparing notes in the writers’ room…",
    "Checking whether this is the reboot…",
    "Waiting for the post-credits scene…",
    "Polishing the ratings montage…",
    "Rolling credits on the paperwork…",
]

# `task_display_mode="plan"` groups these stable task IDs into a single plan. Task titles are
# Slack-owned copy (all <256 chars); Runtime only emits the stable event IDs mapped below.
_PLAN_TASKS = [
    ("deduplication", "Check for an existing watchlist entry"),
    ("notion_creation", "Create the Notion watchlist entry"),
    ("database_search", "Search the watchlist database"),
    ("title_matching", "Find the best title match"),
    ("disambiguation", "Resolve title disambiguation"),
    ("omdb_details", "Fetch OMDb details"),
    ("rotten_tomatoes", "Fetch Rotten Tomatoes ratings"),
    ("source_comparison", "Compare source results"),
    ("verification", "Verify the resolved match"),
    ("confidence", "Assess enrichment confidence"),
    ("notion_update", "Update the Notion watchlist entry"),
]
_TASK_TITLES = dict(_PLAN_TASKS)

# One transport-neutral event may close one task and begin another. `_TaskStream` supplies any
# missing `in_progress` step before a terminal status, keeping every task transition valid even
# when graph routing skips `disambiguate` (the one-candidate path).
_EVENT_TASK_UPDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "deduplication.started": (("deduplication", "in_progress"),),
    "deduplication.duplicate": (("deduplication", "complete"),),
    "deduplication.complete": (
        ("deduplication", "complete"),
        ("notion_creation", "in_progress"),
    ),
    "notion.creation.complete": (
        ("notion_creation", "complete"),
        ("database_search", "in_progress"),
        ("rotten_tomatoes", "in_progress"),
    ),
    "database.read.complete": (
        ("database_search", "complete"),
        ("title_matching", "in_progress"),
    ),
    "omdb.search.complete": (
        ("title_matching", "complete"),
        ("disambiguation", "in_progress"),
    ),
    "title.disambiguation.complete": (
        ("disambiguation", "complete"),
        ("omdb_details", "in_progress"),
    ),
    "omdb.details.complete": (
        ("title_matching", "complete"),
        ("disambiguation", "complete"),
        ("omdb_details", "complete"),
    ),
    "rotten_tomatoes.complete": (("rotten_tomatoes", "complete"),),
    "sources.compare.complete": (
        ("source_comparison", "complete"),
        ("verification", "in_progress"),
    ),
    "match.verification.complete": (
        ("verification", "complete"),
        ("confidence", "in_progress"),
    ),
    "confidence.assessment.complete": (
        ("confidence", "complete"),
        ("notion_update", "in_progress"),
    ),
    "notion.update.complete": (("notion_update", "complete"),),
    # An HITL pause is an intentional terminal point for this stream. `error` makes the
    # blocked task visible; the threaded picker directly beneath it supplies the next action.
    "human_input.required": (("disambiguation", "error"),),
}


class _TaskStream:
    """Best-effort lifecycle for one Slack grouped task plan."""

    def __init__(
        self,
        client: Any,
        *,
        title: str,
        channel: str,
        team_id: str | None,
        user_id: str | None,
        thread_ts: str,
        set_status: Any,
    ) -> None:
        self._client = client
        self._title = title
        self._channel = channel
        self._team_id = team_id
        self._user_id = user_id
        self._thread_ts = thread_ts
        self._set_status = set_status
        self._ts: str | None = None
        self._stopped = False
        self._statuses = dict.fromkeys(_TASK_TITLES, "pending")

    async def start(self) -> None:
        """Open the threaded stream, then set the title-specific rotating status."""
        try:
            response = await self._client.chat_startStream(
                channel=self._channel,
                thread_ts=self._thread_ts,
                recipient_team_id=self._team_id,
                recipient_user_id=self._user_id,
                task_display_mode="plan",
            )
            self._ts = response.get("ts")
        except Exception:  # noqa: BLE001 - Slack UI must never gate durable enrichment
            log.exception("slack: failed to start add task stream for %r", self._title)

        if self._set_status is not None:
            try:
                await self._set_status(
                    status=f"Adding {self._title} to your watchlist…",
                    loading_messages=LOADING_MESSAGES,
                )
            except Exception:  # noqa: BLE001 - status rotation is a best-effort enhancement
                log.exception("slack: failed to set add loading status for %r", self._title)

    async def initialize(self) -> None:
        """Publish the whole pending plan through appendStream."""
        await self._append(
            [self._chunk(task_id, "pending") for task_id, _title in _PLAN_TASKS]
        )

    async def emit(self, event_id: str) -> None:
        """Translate one stable runtime/transport event into task-update transitions."""
        if event_id == "workflow.error":
            chunks: list[dict] = []
            for task_id, status in self._statuses.items():
                if status == "in_progress":
                    chunks.extend(self._transition(task_id, "error"))
            await self._append(chunks)
            return

        updates = _EVENT_TASK_UPDATES.get(event_id)
        if updates is None:
            log.warning("slack: ignoring unknown progress event %r", event_id)
            return
        chunks = []
        for task_id, status in updates:
            chunks.extend(self._transition(task_id, status))
        if (
            self._statuses["omdb_details"] == "complete"
            and self._statuses["rotten_tomatoes"] == "complete"
            and self._statuses["source_comparison"] == "pending"
        ):
            chunks.extend(self._transition("source_comparison", "in_progress"))
        await self._append(chunks)

    def _transition(self, task_id: str, target: str) -> list[dict]:
        current = self._statuses[task_id]
        if current == target or current in {"complete", "error"}:
            return []
        chunks: list[dict] = []
        if current == "pending" and target in {"complete", "error"}:
            self._statuses[task_id] = "in_progress"
            chunks.append(self._chunk(task_id, "in_progress"))
        self._statuses[task_id] = target
        chunks.append(self._chunk(task_id, target))
        return chunks

    @staticmethod
    def _chunk(task_id: str, status: str) -> dict:
        return {
            "type": "task_update",
            "id": task_id,
            "title": _TASK_TITLES[task_id],
            "status": status,
        }

    async def _append(self, chunks: list[dict]) -> None:
        if self._ts is None or not chunks:
            return
        try:
            await self._client.chat_appendStream(
                channel=self._channel,
                ts=self._ts,
                chunks=chunks,
            )
        except Exception:  # noqa: BLE001 - task progress is best-effort
            log.exception("slack: failed to append add task progress for %r", self._title)

    async def stop(self) -> None:
        """Stop an opened stream exactly once, including on pause/failure/duplicate."""
        if self._ts is None or self._stopped:
            return
        self._stopped = True
        try:
            await self._client.chat_stopStream(channel=self._channel, ts=self._ts)
        except Exception:  # noqa: BLE001 - task UI must never change enrichment outcome
            log.exception("slack: failed to stop add task stream for %r", self._title)


class SlackTransport:
    """Bolt Socket Mode app wiring the HITL picker + mention commands to a `Runtime`."""

    def __init__(self, settings: Settings, runtime: Runtime) -> None:
        self._channel = settings.SLACK_CHANNEL
        self._runtime = runtime
        # Socket Mode needs no signing secret (no inbound HTTP). The bot token authorizes
        # chat.postMessage / chat.update; the app token opens the WebSocket.
        self._app = AsyncApp(token=settings.SLACK_BOT_TOKEN)
        self._handler = AsyncSocketModeHandler(self._app, settings.SLACK_APP_TOKEN)
        # Phase 9: mention-based `add` does the work in a background task. Hold strong refs so
        # the tasks aren't garbage-collected mid-flight (asyncio only keeps weak refs).
        self._tasks: set[asyncio.Task[None]] = set()
        self._register()

    def _register(self) -> None:
        @self._app.action(_PICK_ACTION)
        async def handle_pick(ack, body, action, client, logger) -> None:  # noqa: ANN001
            """A candidate button was clicked → resume the paused run with that imdbID."""
            await ack()
            data = json.loads(action["value"])
            page_id, imdb_id = data["page_id"], data["imdb_id"]
            user = body.get("user", {}).get("id")
            picked = action.get("text", {}).get("text") or imdb_id  # the button label
            channel, ts = body["channel"]["id"], body["message"]["ts"]
            logger.info("slack: pick %s for page %s by %s", imdb_id, page_id, user)

            # Immediate feedback: drop the buttons and show progress. resume() re-runs the
            # enrichment tail (resolve_rt → judge → update_notion) — several LLM calls — so the
            # final update can be seconds away; without this the picker would sit unchanged and
            # look broken (and invite confused re-clicks).
            await client.chat_update(
                channel=channel,
                ts=ts,
                text=f"Resolving {picked}…",
                blocks=_section_blocks(f"⏳ <@{user}> picked *{picked}* — resolving…"),
            )

            # resume() is a no-op on an already-finished thread, so a double-click is safe.
            result = await self._runtime.resume(page_id, imdb_id)
            label = result.title or imdb_id
            if result.year:
                label += f" ({result.year})"
            imdb_url = _IMDB_URL.format(imdb_id)
            await client.chat_update(
                channel=channel,
                ts=ts,
                text=f"Resolved: {label} → {result.status}",  # notification fallback
                blocks=_resolved_blocks(user, label, imdb_url, result.status),
                unfurl_links=False,  # the IMDb link is for clicking, not a preview card
                unfurl_media=False,
            )

        @self._app.action(_MANUAL_ACTION)
        async def handle_manual(ack, body, action, client, logger) -> None:  # noqa: ANN001
            """The manual IMDb-link input was submitted (6e) → resume with the pasted id.

            The escape hatch for when the right title isn't among the ≤5 OMDb candidates. The
            graph is still paused at the same `interrupt()`, so a pasted id resumes it exactly
            like a candidate button — the resume path accepts any imdbID.
            """
            await ack()
            channel, ts = body["channel"]["id"], body["message"]["ts"]
            user = body.get("user", {}).get("id")
            block_id = action.get("block_id", "")
            page_id = block_id[len(_MANUAL_BLOCK_PREFIX) :]
            raw = (action.get("value") or "").strip()
            match = _IMDB_ID.search(raw)

            if not page_id or not match:
                # Message inputs have no modal-style inline validation, so nudge via an
                # ephemeral reply and leave the picker untouched — a `chat_update` here would
                # wipe the half-typed field and drop the candidate buttons (ADR 0006 / 6e).
                logger.info(
                    "slack: manual submit for page %s — no imdb id in %r", page_id, raw
                )
                await client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    text=(
                        "Couldn't find an IMDb id in that — paste a link like "
                        "`https://www.imdb.com/title/tt…/` or the bare `tt…` id, "
                        "then hit Enter."
                    ),
                )
                return

            imdb_id = match.group(0)
            logger.info("slack: manual resume %s for page %s by %s", imdb_id, page_id, user)
            # Terminal resolution — safe to replace the picker now (the input was submitted).
            await client.chat_update(
                channel=channel,
                ts=ts,
                text=f"Resolving {imdb_id}…",
                blocks=_section_blocks(f"⏳ <@{user}> entered *{imdb_id}* — resolving…"),
            )
            result = await self._runtime.resume(page_id, imdb_id)
            label = result.title or imdb_id
            if result.year:
                label += f" ({result.year})"
            imdb_url = _IMDB_URL.format(imdb_id)
            await client.chat_update(
                channel=channel,
                ts=ts,
                text=f"Resolved: {label} → {result.status}",  # notification fallback
                blocks=_resolved_blocks(user, label, imdb_url, result.status),
                unfurl_links=False,
                unfurl_media=False,
            )

        @self._app.event("app_mention")
        async def handle_mention(body, event, say, set_status, logger) -> None:  # noqa: ANN001
            """Dispatch `@movie-bot add <title>` and `@movie-bot run`."""
            team_id = body.get("team_id") or event.get("team")
            await self._handle_mention(event, say, logger, set_status, team_id)

    async def _handle_mention(
        self, event, say, logger, set_status=None, team_id: str | None = None
    ) -> None:  # noqa: ANN001
        """Handle one app mention without coupling tests to Bolt's listener registry."""
        parsed = parse_mention_command(event.get("text", ""))
        if parsed is None:
            await say("Mention me with `add <title>` or `run`.")
            return

        command, argument = parsed
        if command == "run":
            logger.info("slack: manual run requested")
            await say("Running a reconcile sweep…")
            summary = await self._runtime.reconcile()
            await say(f"reconcile: {summary}")
            return

        if command != "add":
            await say("Mention me with `add <title>` or `run`.")
            return
        if not argument:
            await say(
                "Usage: `@NotionMovieAgent add <title>` — e.g. `@NotionMovieAgent add Dune`"
            )
            return

        channel = event.get("channel")
        if not channel:  # every app_mention event carries one — defensive only
            await say("Couldn't tell which channel to reply in — try again.")
            return
        # A top-level mention starts a new thread; a mention made inside an existing thread
        # keeps using that thread's root. Slack's assistant status and the eventual answer must
        # target the same root timestamp.
        thread_ts = event.get("thread_ts") or event.get("ts")
        if not thread_ts:  # every real message event carries ts — defensive only
            await say("Couldn't tell which message to reply to — try again.")
            return
        user = event.get("user")
        logger.info("slack: mention add %r from %s in %s", argument, user, channel)
        task = asyncio.create_task(
            self._add_flow(
                argument,
                channel,
                team_id or event.get("team"),
                user,
                thread_ts,
                set_status,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _add_flow(
        self,
        title: str,
        channel: str,
        team_id: str | None,
        user: str | None,
        thread_ts: str,
        set_status,
    ) -> None:  # noqa: ANN001
        """Background worker for mention `add`: dedupe → create + enrich out-of-band (Phase 9).

        Runs after the event listener schedules it. On a dedupe hit it posts "already on your
        watchlist" and creates nothing (ADR 0012 — best-effort dedupe). Otherwise it delegates
        to `Runtime.create_and_enrich`, which creates the page and runs the graph under the
        in-flight guard; the done/failed ping is the graph's `notify` node, so a run that
        pauses for disambiguation still pings this channel on resolve. Progress is a threaded
        `chat.startStream` plan made of `task_update` chunks; the assistant status supplies the
        title-specific loading-message rotation. Final outcomes use `chat.postMessage` with the
        original `thread_ts`, so the channel only gets a thread indicator.
        """

        stream = _TaskStream(
            self._app.client,
            title=title,
            channel=channel,
            team_id=team_id,
            user_id=user,
            thread_ts=thread_ts,
            set_status=set_status,
        )
        await stream.start()
        await stream.initialize()

        try:
            await stream.emit("deduplication.started")
            existing = await self._runtime.find_duplicate(title)
            if existing is not None:
                await stream.emit("deduplication.duplicate")
                text = (
                    f"*{existing.title}* is already on the watchlist "
                    f"(status: *{existing.status or 'unset'}*) — nothing added."
                )
                await self._app.client.chat_postMessage(
                    channel=channel, text=text, thread_ts=thread_ts
                )
                return
            await stream.emit("deduplication.complete")
            await self._runtime.create_and_enrich(
                title,
                channel=channel,
                user=user,
                # Persist the request thread root with the graph. If enrichment pauses for
                # human input, its eventual completion still replies to the original mention.
                thread_ts=thread_ts,
                progress=stream.emit,
            )
        except Exception:
            log.exception("slack: add %r failed", title)
            await stream.emit("workflow.error")
            with contextlib.suppress(Exception):
                text = f"Couldn't add *{title}* — something went wrong. Try again?"
                await self._app.client.chat_postMessage(
                    channel=channel, text=text, thread_ts=thread_ts
                )
        finally:
            await stream.stop()

    async def post_completion(
        self, channel: str, text: str, thread_ts: str | None = None
    ) -> None:
        """Post an `add` completion ping (bound into `Runtime`'s terminal `notify` node).

        A run may only resolve days later — after a Slack disambiguation click or the 6d
        auto-resolve — so durable graph state retains the channel and original mention's
        thread timestamp. New runs post into that thread; older/checkpoint-less runs fall back
        to a channel message. Unfurls are suppressed so the IMDb link stays compact.
        """
        if thread_ts is not None:
            try:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    text=text,
                    thread_ts=thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                return
            except Exception:  # noqa: BLE001 — deleted/stale root → post the result anew
                log.exception("slack: failed to post threaded completion; posting to channel")
        await self._app.client.chat_postMessage(
            channel=channel,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )

    async def post_picker(
        self,
        page_id: str,
        payload: dict,
        origin_channel: str | None = None,
        origin_thread_ts: str | None = None,
    ) -> None:
        """Post a picker in its originating thread, or the configured fallback channel."""
        title = payload.get("title") or "a title"
        kwargs: dict[str, Any] = {
            "channel": origin_channel or self._channel,
            "text": f"Need a hand disambiguating {title}",  # notification fallback
            "blocks": build_picker_blocks(page_id, payload),
            # The candidate IMDb links are for clicking, not previewing — suppress unfurls so
            # the picker stays compact (no poster/summary cards stacked under each option).
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if origin_channel is not None and origin_thread_ts is not None:
            kwargs["thread_ts"] = origin_thread_ts
        await self._app.client.chat_postMessage(**kwargs)
        log.info("slack: posted disambiguation picker for page %s", page_id)

    async def start(self) -> None:
        """Open the Socket Mode WebSocket and process events until cancelled."""
        log.info("slack: starting Socket Mode listener")
        await self._handler.start_async()

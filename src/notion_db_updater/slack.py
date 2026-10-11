"""Slack Bolt (Socket Mode) transport — the app's only inbound path (ADR 0009 / 0010).

A single outbound WebSocket carries all inbound interaction, so there's no public HTTP endpoint
(works in local Docker). Three jobs:

1. **HITL disambiguation (ADR 0006):** when a reconcile run pauses at `interrupt()`, `Runtime`
   calls `post_picker` — a Block Kit prompt of the OMDb candidates is posted to
   `#notion-movie-db`. The button click resumes the paused graph with the chosen imdbID.
2. **Watchlist conversations:** app mentions and replies in the originating thread go through
   the conversational agent. Its tools read Watchlist data, stage user-requested changes, and
   run an explicit reconcile sweep through the shared `Runtime`.

`Runtime` and this transport are mutually referential — the sweep posts *to* Slack (via
`Runtime.set_notifier(post_picker)` + `Runtime.bind_completion_notifier(post_completion)`) and
Slack interactions call *back into* the conversation tools and `Runtime.resume`. `_serve` wires
the notifier, then runs the cron loop and the Socket Mode listener concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Coroutine
from html import escape
from typing import TYPE_CHECKING, Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from .config import Settings
from .omdb import imdb_identity

if TYPE_CHECKING:
    from .app import Runtime

log = logging.getLogger(__name__)

# Block Kit / Slack limits: at most 5 candidates rendered (one section + one button each);
# button text is capped well under Slack's 75-char limit.
_MAX_CANDIDATES = 5
_BUTTON_LABEL_MAX = 70
_PICK_ACTION = re.compile(r"^pick:\d+$")  # one action_id per button: pick:0 … pick:4
_APPROVAL_ACTION = re.compile(r"^watchlist:(approve|deny)$")
_SELECTION_ACTION = "watchlist:selection"
_IDENTITY_ACTION = re.compile(r"^identity:(confirm|deny)$")
_CONVERSATION_PICK_ACTION = re.compile(r"^conversation:pick:\d+$")
_CONVERSATION_MANUAL_ACTION = "conversation:manual"


def _button(action_id: str, label: str, value: str, *, primary: bool = False) -> dict:
    button = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": label},
        "value": value,
    }
    if primary:
        button["style"] = "primary"
    return button


def _context_block(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _identity_confirmation_blocks(confirmation: dict, thread_ts: str) -> list[dict]:
    identity = confirmation["identity"]
    title = escape(identity["title"], quote=False)
    existing = escape(confirmation["existing_title"], quote=False)
    page_id = confirmation["page_id"].replace("-", "")
    year = identity.get("year") or "Year unknown"
    kind = identity.get("media_type") or "Type unknown"
    imdb_id = identity["imdb_id"]
    value = json.dumps(
        {
            "request_key": confirmation["request_key"],
            "token": confirmation["token"],
            "thread_ts": thread_ts,
        }
    )
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Confirm an existing Watchlist Entry"},
        },
        *_section_blocks(
            f"Your Watchlist already contains <https://www.notion.so/{page_id}|{existing}>, "
            "but its IMDb ID is missing.\n"
            f"Is it the same work as *{title}* ({year}) · {kind}?\n"
            f"<https://www.imdb.com/title/{imdb_id}/|View IMDb match>"
        ),
        _context_block(
            "Confirming saves the IMDb ID on this existing Entry. "
            "Your other fields stay unchanged and no duplicate is added. "
            "Expires in 30 minutes."
        ),
        {
            "type": "actions",
            "elements": [
                _button("identity:confirm", "Yes, same movie or show", value, primary=True),
                _button("identity:deny", "Not a match", value),
            ],
        },
    ]


def _approval_blocks(response: str, thread_ts: str) -> list[dict]:
    """Render the persisted preview with selectable changes and decision buttons."""
    token = re.search(r"`approve ([0-9a-f]{16})`", response)
    if token is None:
        return _section_blocks(response)
    lines = [line for line in response.splitlines() if re.match(r"^\d+\. ", line)]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Review Watchlist changes"}}
    ]
    options = []
    labels = {
        "add": "Add",
        "remove": "Remove",
        "watched": "Mark watched",
        "unwatched": "Mark unwatched",
        "refresh": "Refresh enrichment",
    }
    for index, line in enumerate(lines, 1):
        label = re.sub(
            r"^\d+\. (\w+):",
            lambda m, index=index: f"{index}. {labels.get(m[1], m[1])} —",
            line,
        )
        heading, description = label.split(" —", 1)
        blocks.extend(
            _section_blocks(
                f"*{escape(heading, quote=False)}* —{escape(description, quote=False)}"
            )
        )
        options.append(
            {"text": {"type": "plain_text", "text": label[:75]}, "value": str(index)}
        )
    blocks.append(
        _context_block(
            "Select the changes to apply, then approve. "
            "Deny cancels the whole plan. This approval expires in 30 minutes."
        )
    )
    blocks.append(
        {
            "type": "actions",
            "block_id": "watchlist_selection",
            "elements": [
                {
                    "type": "multi_static_select",
                    "action_id": _SELECTION_ACTION,
                    "placeholder": {"type": "plain_text", "text": "Select changes"},
                    "options": options,
                    "initial_options": options,
                }
            ],
        }
    )
    for notice in response.splitlines()[len(lines) + 2 :]:
        if notice.strip():
            blocks.extend(_section_blocks(escape(notice, quote=False)))
    value = json.dumps({"token": token[1], "thread_ts": thread_ts})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                _button("watchlist:approve", "Approve selected", value, primary=True),
                _button("watchlist:deny", "Deny", value),
            ],
        }
    )
    return blocks


# The manual-input escape hatch (6e): deliberately outside the `pick:\d+` regex so the
# candidate-button handler can never receive it. `block_id` carries the page_id (see below).
_MANUAL_ACTION = "manual:submit"
_MANUAL_BLOCK_PREFIX = "manual_input:"
_IMDB_ID = re.compile(r"tt\d+")  # matches a full imdb.com/title/tt… URL or a bare tt… id

# IMDb title page for a given imdbID — rendered as a clickable link in the picker.
_IMDB_URL = "https://www.imdb.com/title/{}/"


def _fmt_year(year: str | int | None) -> str:
    """OMDb `Year` for display: a plain movie year ("2021") or a series range ("2013–2017").

    An open-ended range ("2013–", ongoing series) reads better as "2013–present".
    """
    y = str(year or "").strip()
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
    title = escape(payload.get("title") or "(unknown title)", quote=False)
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
        bits = f"*{escape(label, quote=False)}*"
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


def _conversation_picker_blocks(choice: dict, thread_ts: str) -> list[dict]:
    """Reuse the enrichment card with conversation-bound controls."""
    blocks = build_picker_blocks("", choice)
    binding = {
        "request_key": choice["request_key"],
        "token": choice["token"],
        "thread_ts": thread_ts,
    }
    for block in blocks:
        if block["type"] == "actions":
            for button in block["elements"]:
                identity = json.loads(button["value"])["imdb_id"]
                button["action_id"] = "conversation:" + button["action_id"]
                button["value"] = json.dumps(binding | {"imdb_id": identity})
        elif block["type"] == "input":
            block["block_id"] = json.dumps(binding)
            block["element"]["action_id"] = _CONVERSATION_MANUAL_ACTION
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


class SlackTransport:
    """Bolt Socket Mode app wiring the HITL picker + Watchlist conversations to a Runtime."""

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
        @self._app.action(_CONVERSATION_PICK_ACTION)
        @self._app.action(_CONVERSATION_MANUAL_ACTION)
        async def handle_conversation_pick(ack, body, action, client) -> None:  # noqa: ANN001
            await ack()
            self._schedule(self._handle_conversation_pick(body, action, client))

        @self._app.action(_IDENTITY_ACTION)
        async def handle_identity(ack, body, action, client) -> None:  # noqa: ANN001
            await ack()
            self._schedule(self._handle_identity_confirmation(body, action, client))

        @self._app.action(_SELECTION_ACTION)
        async def handle_selection(ack) -> None:  # noqa: ANN001
            await ack()

        @self._app.action(_APPROVAL_ACTION)
        async def handle_approval(ack, body, action, client) -> None:  # noqa: ANN001
            await ack()
            self._schedule(self._handle_approval(body, action, client))

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
            await self._resolve_picker(page_id, imdb_id, user, channel, ts, client)

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
            await self._resolve_picker(page_id, imdb_id, user, channel, ts, client)

        @self._app.event("app_mention")
        async def handle_mention(body, event, say) -> None:  # noqa: ANN001
            """Dispatch app mentions through the conversational assistant."""
            team_id = body.get("team_id") or event.get("team")
            await self._handle_mention(event, say, team_id)

        @self._app.event("message")
        async def handle_thread(body, event, say) -> None:  # noqa: ANN001
            if not event.get("thread_ts") or re.match(r"^\s*<@", event.get("text", "")):
                return
            if getattr(self._runtime, "conversation", None) is not None:
                self._schedule_conversation(
                    event, say, body.get("team_id") or event.get("team")
                )

    async def _resolve_picker(self, page_id, imdb_id, user, channel, ts, client) -> None:
        result = await self._runtime.resume(page_id, imdb_id)
        label = result.title or imdb_id
        if result.year:
            label += f" ({result.year})"
        await client.chat_update(
            channel=channel,
            ts=ts,
            text=f"Resolved: {label} → {result.status}",
            blocks=_resolved_blocks(user, label, _IMDB_URL.format(imdb_id), result.status),
            unfurl_links=False,
            unfurl_media=False,
        )

    async def _handle_mention(self, event, say, team_id: str | None = None) -> None:  # noqa: ANN001
        """Handle one app mention without coupling tests to Bolt's listener registry."""
        conversation = getattr(self._runtime, "conversation", None)
        if conversation is None:
            await say(
                "The conversational Watchlist assistant is not configured. "
                "Set OPENAI_AGENT_MODEL to enable it."
            )
            return
        self._schedule_conversation(event, say, team_id)

    def _schedule_conversation(self, event, say, team_id=None) -> None:  # noqa: ANN001
        self._schedule(self.handle_conversation(event, say, team_id))

    def _schedule(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_conversation(self, event, say, team_id=None) -> None:  # noqa: ANN001
        """Reply in the originating thread using the durable conversation Runtime."""
        conversation = self._runtime.conversation
        assert conversation is not None
        if (
            event.get("bot_id")
            or event.get("subtype")
            or not all(event.get(k) for k in ("channel", "ts", "text"))
        ):
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        status = "active"
        progress_ts = None
        replied = False
        ignored = False
        heartbeat = None
        try:
            message = await self._app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="💭 Working on your request…",
            )
            if isinstance(message["ts"], str):
                progress_ts = message["ts"]
        except Exception:
            log.exception("Could not post conversation progress")

        async def progress(text: str) -> None:
            if progress_ts:
                await self._app.client.chat_update(
                    channel=channel,
                    ts=progress_ts,
                    text=escape(text, quote=False),
                    blocks=[],
                )

        async def reply(**kwargs) -> None:
            nonlocal replied
            if progress_ts and not replied:
                await self._app.client.chat_update(
                    channel=channel,
                    ts=progress_ts,
                    text=kwargs["text"],
                    blocks=kwargs.get("blocks", []),
                )
            else:
                await say(**kwargs)
            replied = True

        async def keep_status() -> None:
            while True:
                await asyncio.sleep(60)
                await self._conversation_status(channel, thread_ts, "processing")

        try:
            await self._conversation_status(channel, thread_ts, "processing")
            heartbeat = asyncio.create_task(keep_status())
            response = await conversation.handle(event, team_id, progress=progress)
            if response is None:
                ignored = True
                return
            for payload in self._conversation_replies(response, event, team_id):
                if payload.get("blocks"):
                    status = "suspended"
                await reply(**payload)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if progress_ts and not replied:
                try:
                    if ignored:
                        await self._app.client.chat_delete(channel=channel, ts=progress_ts)
                    else:
                        await self._app.client.chat_update(
                            channel=channel,
                            ts=progress_ts,
                            blocks=[],
                            text="The request ended without a reply. "
                            "Please retry or clarify your request.",
                        )
                except Exception:
                    log.exception("Could not finalize conversation progress")
            await self._conversation_status(channel, thread_ts, status)

    def _conversation_replies(
        self, response: str, event: dict, team_id: str | None
    ) -> list[dict]:
        thread_ts = event.get("thread_ts") or event["ts"]
        conversation = self._runtime.conversation
        get_choices = getattr(conversation, "disambiguations", None)
        choices = get_choices(event, team_id) if get_choices else []
        replies = []
        if response.startswith("Approve this exact action list"):
            replies.append(
                {
                    "text": re.sub(
                        r"^Reply `approve .*?$",
                        "Use the Approve or Deny buttons below.",
                        response,
                        flags=re.MULTILINE,
                    ),
                    "thread_ts": thread_ts,
                    "blocks": _approval_blocks(response, thread_ts),
                }
            )
        elif not choices:
            replies.append({"text": response, "thread_ts": thread_ts})
        replies.extend(
            {
                "text": f"Which {choice['title']} did you mean?",
                "thread_ts": thread_ts,
                "blocks": _conversation_picker_blocks(choice, thread_ts),
            }
            for choice in choices
        )
        replies.extend(self._identity_replies(event, team_id))
        return replies

    async def _handle_conversation_pick(self, body, action, client) -> None:  # noqa: ANN001
        manual = action["action_id"] == _CONVERSATION_MANUAL_ACTION
        data = json.loads(action["block_id"] if manual else action["value"])
        identity = imdb_identity(action.get("value", "")) if manual else data["imdb_id"]
        channel = body["channel"]["id"]
        if identity is None:
            await client.chat_postEphemeral(
                channel=channel,
                user=body["user"]["id"],
                text="Paste an IMDb title link or a bare tt… ID.",
            )
            return
        event = {
            "channel": channel,
            "user": body["user"]["id"],
            "thread_ts": data["thread_ts"],
            "ts": f"choice:{data['token']}",
        }
        team = body.get("team", {}).get("id")
        status = "active"
        await self._conversation_status(channel, event["thread_ts"], "processing")
        try:
            conversation = self._runtime.conversation
            assert conversation is not None
            response = await conversation.choose_candidate(
                data["request_key"],
                data["token"],
                identity,
                event,
                team,
                manual=manual,
            )
            if response is not None:
                for index, reply in enumerate(
                    self._conversation_replies(response, event, team)
                ):
                    if reply.get("blocks"):
                        status = "suspended"
                    if index == 0:
                        await client.chat_update(
                            channel=channel,
                            ts=body["message"]["ts"],
                            text=reply["text"],
                            blocks=reply.get("blocks", []),
                        )
                    else:
                        await client.chat_postMessage(channel=channel, **reply)
        finally:
            await self._conversation_status(channel, event["thread_ts"], status)

    def _identity_replies(self, event: dict, team_id: str | None) -> list[dict]:
        get_confirmations = getattr(self._runtime.conversation, "identity_confirmations", None)
        confirmations = get_confirmations(event, team_id) if get_confirmations else []
        thread_ts = event.get("thread_ts") or event["ts"]
        return [
            {
                "text": (
                    f"Does your existing Entry {c['existing_title']} match "
                    f"{c['identity']['title']} "
                    f"({c['identity'].get('year') or 'year unknown'})? "
                    "Confirm to save its IMDb ID, or reject to leave it unchanged."
                ),
                "thread_ts": thread_ts,
                "blocks": _identity_confirmation_blocks(c, thread_ts),
            }
            for c in confirmations
        ]

    async def _handle_identity_confirmation(self, body, action, client) -> None:  # noqa: ANN001
        decision = action["action_id"]
        if decision not in {"identity:confirm", "identity:deny"}:
            return
        data = json.loads(action["value"])
        channel, thread_ts = body["channel"]["id"], data["thread_ts"]
        event = {
            "user": body.get("user", {}).get("id"),
            "channel": channel,
            "thread_ts": thread_ts,
        }
        conversation = self._runtime.conversation
        assert conversation is not None
        team_id = body.get("team", {}).get("id")
        status = "active"
        await self._conversation_status(channel, thread_ts, "processing")
        try:
            response = await conversation.decide_identity(
                data["request_key"],
                data["token"],
                event,
                team_id,
                confirmed=decision == "identity:confirm",
            )
            if response is not None:
                source_event = event | {"ts": data["request_key"].rsplit(":", 1)[-1]}
                pending = next(
                    (
                        c
                        for c in conversation.identity_confirmations(source_event, team_id)
                        if c["token"] == data["token"]
                    ),
                    None,
                )
                blocks = _section_blocks(response)
                if pending:
                    status = "suspended"
                    blocks += _identity_confirmation_blocks(pending, thread_ts)
                await client.chat_update(
                    channel=channel,
                    ts=body["message"]["ts"],
                    text=response,
                    blocks=blocks,
                )
        finally:
            await self._conversation_status(channel, thread_ts, status)

    async def _handle_approval(self, body, action, client) -> None:  # noqa: ANN001
        """Buttons reuse the durable approval path without invoking the model."""
        user = body.get("user", {}).get("id")
        data = json.loads(action["value"])
        token, thread_ts = data["token"], data["thread_ts"]
        decision = action["action_id"].split(":")[-1]
        if decision not in {"approve", "deny"} or not re.fullmatch(r"[0-9a-f]{16}", token):
            return
        channel = body["channel"]["id"]
        text = f"{decision} {token}"
        if decision == "approve":
            selection = (
                body.get("state", {})
                .get("values", {})
                .get("watchlist_selection", {})
                .get(_SELECTION_ACTION)
            )
            if selection is not None:
                selected = selection.get("selected_options") or []
                if not selected:
                    await client.chat_postEphemeral(
                        channel=channel,
                        user=user,
                        text="Select at least one change, or deny the plan.",
                    )
                    return
                numbers = [option["value"] for option in selected]
                if not all(re.fullmatch(r"[1-9]\d*", number) for number in numbers):
                    return
                text += " " + ",".join(numbers)
        action_ts = action.get("action_ts", body["message"]["ts"])
        event = {
            "user": user,
            "channel": channel,
            "thread_ts": thread_ts,
            "ts": f"decision:{token}:{decision}:{action_ts}",
            "text": text,
        }
        await self._conversation_status(channel, thread_ts, "processing")
        status = "active"
        try:
            conversation = self._runtime.conversation
            assert conversation is not None
            response = await conversation.handle(event, body.get("team", {}).get("id"))
            if response is not None:
                await client.chat_update(
                    channel=channel,
                    ts=body["message"]["ts"],
                    text=response,
                    blocks=_section_blocks(response),
                )
                for reply in self._identity_replies(event, body.get("team", {}).get("id")):
                    status = "suspended"
                    await client.chat_postMessage(channel=channel, **reply)
        finally:
            await self._conversation_status(channel, thread_ts, status)

    async def _conversation_status(self, channel: str, thread_ts: str, status: str) -> None:
        """Native Slack loading UI is best effort and never blocks a Watchlist request."""
        try:
            # Use api_call until the installed SDK exposes the agent sessions method.
            await self._app.client.api_call(
                "agents.sessions.setStatus",
                json={"channel_id": channel, "thread_ts": thread_ts, "status": status},
            )
        except SlackApiError as exc:
            error = exc.response.get("error")
            if error not in {
                "feature_disabled",
                "unknown_method",
                "method_not_supported_for_channel_type",
            }:
                log.warning("Slack agent session status rejected: %s", error)
                return
            log.info("Slack agent session status unavailable (%s); using thread status", error)
            try:
                await self._app.client.assistant_threads_setStatus(
                    channel_id=channel,
                    thread_ts=thread_ts,
                    status="is working…" if status == "processing" else "",
                )
            except Exception:
                log.exception("Could not update Slack assistant thread status")
        except Exception:
            log.exception("Could not update Slack agent session status")

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
        if self._runtime.conversation is not None:
            for reply in await self._runtime.conversation.recover():
                confirmations = reply.pop("identity_confirmations", [])
                await self._app.client.chat_postMessage(**reply)
                for confirmation in confirmations:
                    await self._app.client.chat_postMessage(
                        **reply,
                        blocks=_identity_confirmation_blocks(confirmation, reply["thread_ts"]),
                    )
                if confirmations:
                    await self._conversation_status(
                        reply["channel"], reply["thread_ts"], "suspended"
                    )
        log.info("slack: starting Socket Mode listener")
        await self._handler.start_async()

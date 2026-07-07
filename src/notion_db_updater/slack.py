"""Slack Bolt (Socket Mode) transport — the app's only inbound path (ADR 0009 / 0010).

A single outbound WebSocket carries all inbound interaction, so there's no public HTTP endpoint
(works in local Docker). Three jobs:

1. **HITL disambiguation (ADR 0006):** when a reconcile run pauses at `interrupt()`, `Runtime`
   calls `post_picker` — a Block Kit prompt of the OMDb candidates is posted to
   `#notion-movie-db`. The button click resumes the paused graph with the chosen imdbID.
2. **Manual run:** an `@movie-bot run` app-mention triggers `reconcile()` (under the same
   single-flight lock as the cron; ADR 0001).
3. **`/add <title>` (Phase 9 / ADR 0012):** a slash command originates an Entry from Slack. The
   handler `ack()`s within Slack's 3-second window (an ephemeral "Adding…") and does the
   dedupe / create / out-of-band enrich in a background task; the completion ping is posted by
   the graph's terminal `notify` node (via `post_completion`, bound into `Runtime`), because a
   run may only reach `done`/`failed` much later — after a disambiguation click or the 6d
   auto-resolve.

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


class SlackTransport:
    """Bolt Socket Mode app wiring the HITL picker + manual-run mention to a `Runtime`."""

    def __init__(self, settings: Settings, runtime: Runtime) -> None:
        self._channel = settings.SLACK_CHANNEL
        self._runtime = runtime
        # Socket Mode needs no signing secret (no inbound HTTP). The bot token authorizes
        # chat.postMessage / chat.update; the app token opens the WebSocket.
        self._app = AsyncApp(token=settings.SLACK_BOT_TOKEN)
        self._handler = AsyncSocketModeHandler(self._app, settings.SLACK_APP_TOKEN)
        # Phase 9: `/add` acks fast and does the work in a background task. Hold strong refs so
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
        async def handle_mention(event, say, logger) -> None:  # noqa: ANN001
            """`@movie-bot run` → one reconcile sweep (single-flight, ADR 0001)."""
            if "run" not in event.get("text", "").lower():
                await say("Mention me with `run` to trigger a reconcile sweep.")
                return
            logger.info("slack: manual run requested")
            await say("Running a reconcile sweep…")
            summary = await self._runtime.reconcile()
            await say(f"reconcile: {summary}")

        @self._app.command("/add")
        async def handle_add(ack, command, logger) -> None:  # noqa: ANN001
            """`/add <title>` → create the Entry and enrich it out-of-band (Phase 9).

            Acks within Slack's 3-second window with an ephemeral "Adding…", then hands the
            dedupe / create / enrich to a background task so the socket isn't blocked. The
            terminal completion ping comes from the graph's `notify` node, not from here (the
            run may resolve much later, after a disambiguation click or the 6d timeout).
            """
            title = (command.get("text") or "").strip()
            if not title:
                await ack(text="Usage: `/add <title>` — e.g. `/add Dune`")
                return
            channel = command.get("channel_id")
            if not channel:  # every slash command carries one — defensive only
                await ack(text="Couldn't tell which channel to reply in — try again.")
                return
            await ack(text=f"Adding *{title}*… I'll post the result here when it's enriched.")
            user = command.get("user_id")
            logger.info("slack: /add %r from %s in %s", title, user, channel)
            task = asyncio.create_task(self._add_flow(title, channel, user))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _add_flow(self, title: str, channel: str, user: str | None) -> None:
        """Background worker for `/add`: dedupe → create + enrich out-of-band (Phase 9).

        Runs outside the 3-second ack window. On a dedupe hit it posts "already on your
        watchlist" and creates nothing (ADR 0012 — best-effort dedupe). Otherwise it delegates
        to `Runtime.create_and_enrich`, which creates the page and runs the graph under the
        in-flight guard; the done/failed ping is the graph's `notify` node, so a run that
        pauses for disambiguation (picker in `#notion-movie-db`) still pings this channel on
        resolve. Uses `chat_postMessage` (never the slash `response_url`, which expires).
        """
        try:
            existing = await self._runtime.find_duplicate(title)
            if existing is not None:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    text=(
                        f"*{existing.title}* is already on the watchlist "
                        f"(status: *{existing.status or 'unset'}*) — nothing added."
                    ),
                )
                return
            await self._runtime.create_and_enrich(title, channel=channel, user=user)
        except Exception:
            log.exception("slack: /add %r failed", title)
            with contextlib.suppress(Exception):
                await self._app.client.chat_postMessage(
                    channel=channel,
                    text=f"Couldn't add *{title}* — something went wrong. Try again?",
                )

    async def post_completion(self, channel: str, text: str) -> None:
        """Post a `/add` completion ping (bound into `Runtime`'s terminal `notify` node).

        `chat_postMessage` (not the slash `response_url`, which expires) so a run that only
        resolves days later — after a Slack disambiguation click or the 6d auto-resolve — still
        reaches the user. Unfurls suppressed so the IMDb link stays a compact click-through.
        """
        await self._app.client.chat_postMessage(
            channel=channel,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )

    async def post_picker(self, page_id: str, payload: dict) -> None:
        """Post the candidate picker to the channel — the `Runtime` interrupt notifier."""
        title = payload.get("title") or "a title"
        await self._app.client.chat_postMessage(
            channel=self._channel,
            text=f"Need a hand disambiguating {title}",  # notification fallback
            blocks=build_picker_blocks(page_id, payload),
            # The candidate IMDb links are for clicking, not previewing — suppress unfurls so
            # the picker stays compact (no poster/summary cards stacked under each option).
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("slack: posted disambiguation picker for page %s", page_id)

    async def start(self) -> None:
        """Open the Socket Mode WebSocket and process events until cancelled."""
        log.info("slack: starting Socket Mode listener")
        await self._handler.start_async()

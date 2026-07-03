"""Slack Bolt (Socket Mode) transport — the app's only inbound path (ADR 0009 / 0010).

A single outbound WebSocket carries all inbound interaction, so there's no public HTTP endpoint
(works in local Docker). Two jobs:

1. **HITL disambiguation (ADR 0006):** when a reconcile run pauses at `interrupt()`, `Runtime`
   calls `post_picker` — a Block Kit prompt of the OMDb candidates is posted to
   `#notion-movie-db`. The button click resumes the paused graph with the chosen imdbID.
2. **Manual run:** an `@movie-bot run` app-mention triggers `reconcile()` (under the same
   single-flight lock as the cron; ADR 0001).

`Runtime` and this transport are mutually referential — the sweep posts *to* Slack (via
`Runtime.set_notifier(post_picker)`) and Slack button clicks call *back into*
`Runtime.resume` / `.reconcile`. `_serve` wires both, then runs the cron loop and the Socket
Mode listener concurrently.
"""

from __future__ import annotations

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
    """
    title = payload.get("title") or "(unknown title)"
    best_guess = payload.get("best_guess_imdb_id")
    candidates = (payload.get("candidates") or [])[:_MAX_CANDIDATES]

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Which _{title}_ did you mean?*"},
        }
    ]
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
    blocks.append({"type": "actions", "block_id": "pick_actions", "elements": buttons})
    return blocks


def _section_blocks(text: str) -> list[dict]:
    """A one-section mrkdwn message (used to replace the picker as it resolves)."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


class SlackTransport:
    """Bolt Socket Mode app wiring the HITL picker + manual-run mention to a `Runtime`."""

    def __init__(self, settings: Settings, runtime: Runtime) -> None:
        self._channel = settings.SLACK_CHANNEL
        self._runtime = runtime
        # Socket Mode needs no signing secret (no inbound HTTP). The bot token authorizes
        # chat.postMessage / chat.update; the app token opens the WebSocket.
        self._app = AsyncApp(token=settings.SLACK_BOT_TOKEN)
        self._handler = AsyncSocketModeHandler(self._app, settings.SLACK_APP_TOKEN)
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
                blocks=_section_blocks(
                    f"✅ <@{user}> picked *{label}*  ·  <{imdb_url}|IMDb ↗>\n"
                    f"_Enrichment status: *{result.status}*_"
                ),
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

    async def post_picker(self, page_id: str, payload: dict) -> None:
        """Post the candidate picker to the channel — the `Runtime` interrupt notifier."""
        title = payload.get("title") or "a title"
        await self._app.client.chat_postMessage(
            channel=self._channel,
            text=f"Need a hand disambiguating {title}",  # notification fallback
            blocks=build_picker_blocks(page_id, payload),
        )
        log.info("slack: posted disambiguation picker for page %s", page_id)

    async def start(self) -> None:
        """Open the Socket Mode WebSocket and process events until cancelled."""
        log.info("slack: starting Socket Mode listener")
        await self._handler.start_async()

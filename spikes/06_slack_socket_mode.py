"""Spike 06 — Slack Bolt Socket Mode: post a Block Kit button, log the click payload.

The HITL transport (ADR 0010) is Slack Bolt in Socket Mode — one outbound WebSocket, no
public endpoint (works in local Docker). This spike confirms the full wiring before Phase 6c:
  - xoxb- bot token can post to the channel
  - xapp- app token + connections:write opens the socket
  - Interactivity is enabled, so a button click is delivered back over the socket

It posts a message with two candidate buttons (mirroring the disambiguation picker, where
`value` encodes page_id + imdbID), then blocks on the socket and prints each click payload.
This is the shape Phase 6c turns into `graph.invoke(Command(resume=imdbID), thread_id=page_id)`.

Needs SLACK_BOT_TOKEN (xoxb-) and SLACK_APP_TOKEN (xapp-, scope connections:write).
SLACK_CHANNEL defaults to #notion-movie-db.

Run (long-running — click the button in Slack, then Ctrl-C):
    uv run python spikes/06_slack_socket_mode.py
"""

from __future__ import annotations

import json

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import _env

ACTION_ID = "spike_pick_candidate"


def build_blocks() -> list[dict]:
    # Mirrors the real picker: a section + buttons whose `value` encodes page_id + imdbID.
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*[spike]* Which match for *Dune*?"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dune (2021)"},
                    "value": json.dumps({"page_id": "page-123", "imdb_id": "tt1160419"}),
                    "action_id": f"{ACTION_ID}_a",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dune (1984)"},
                    "value": json.dumps({"page_id": "page-123", "imdb_id": "tt0087182"}),
                    "action_id": f"{ACTION_ID}_b",
                },
            ],
        },
    ]


def main() -> None:
    bot_token, app_token = _env.require("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
    channel = _env.get("SLACK_CHANNEL", "#notion-movie-db")

    app = App(token=bot_token)

    # One handler for both buttons (action_id prefix match via regex).
    import re

    @app.action(re.compile(rf"{ACTION_ID}_.*"))
    def handle_pick(ack, body, logger):  # noqa: ANN001
        ack()  # must ack within 3s
        value = body["actions"][0]["value"]
        user = body["user"]["username"]
        print("\n=== BUTTON CLICK RECEIVED OVER SOCKET ===")
        print(f"  clicked by : {user}")
        print(f"  value      : {value}  (this is what Phase 6c feeds Command(resume=...))")
        print("=========================================\n")
        logger.info("pick payload: %s", value)

    # Post the message first, so there's something to click.
    resp = app.client.chat_postMessage(
        channel=channel, text="[spike] disambiguation picker", blocks=build_blocks()
    )
    print(f"posted picker to {channel} (ts={resp['ts']}). Click a button in Slack…")
    print("socket starting; Ctrl-C to stop.\n")

    SocketModeHandler(app, app_token).start()  # blocks on the WebSocket


if __name__ == "__main__":
    main()

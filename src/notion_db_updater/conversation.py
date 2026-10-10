"""Personal Slack conversations and application-owned Watchlist action plans."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Action(BaseModel):
    operation: Literal["add", "remove", "watched", "unwatched", "refresh"]
    imdb_id: str | None = None
    page_id: str | None = None
    reference: int | None = Field(default=None, ge=1)


class Reply(BaseModel):
    text: str
    references: list[str] = Field(default_factory=list)


@dataclass
class RequestTools:
    runtime: Any
    request: dict
    references: list[str | None]
    actions: list[Action] = field(default_factory=list)
    research: Any = None
    persist: Callable[[], None] = lambda: None
    max_searches: int = 3
    max_titles: int = 25
    notices: list[str] = field(default_factory=list)
    progress: Callable[[str], Awaitable[None]] | None = None

    def stage(self, action: Action) -> str:
        """Propose an action; staging never performs a Watchlist write."""
        if len(self.actions) >= self.max_titles:
            notice = (
                f"Action limit reached ({self.max_titles}); request an explicit continuation."
            )
            self.notices.append(notice)
            return notice
        if self.request.get("import_failed"):
            return "The supplied page could not be read; no guessed import is permitted."
        self.actions.append(action)
        return "Staged for the complete request plan; nothing written."

    async def watchlist(self, unwatched: bool = False) -> list[dict]:
        await _report_progress(self.progress, "📚 Reading your Watchlist…")
        entries = await self.runtime.list_watchlist(unwatched=unwatched)
        return [
            asdict(e) if is_dataclass(e) and not isinstance(e, type) else dict(vars(e))
            for e in entries
            if not e.removed and (not unwatched or e.watched is False)
        ]

    async def reconcile(self) -> str:
        """Run one explicitly requested enrichment sweep through the shared Runtime."""
        await _report_progress(self.progress, "🎬 Enriching pending Watchlist Entries…")
        return str(await self.runtime.reconcile())

    async def resolve_title(self, query: str, media_type: str | None = None) -> list[dict]:
        await _report_progress(self.progress, f"🔎 Finding an IMDb match for {query[:180]}…")
        return await self.runtime.resolve_title(query, media_type)

    async def reference(self, index: int) -> dict | str:
        if not 1 <= index <= len(self.references):
            return "That number does not refer to a previously displayed Entry."
        entries = await self.watchlist()
        return next(
            (e for e in entries if e["page_id"] == self.references[index - 1]),
            "That Entry is no longer available.",
        )

    async def search(self, query: str) -> list[dict] | str:
        if not self.research:
            return "Web search is not configured."
        count = self.request.get("searches", 0)
        if count >= self.max_searches:
            notice = (
                f"Search limit reached ({self.max_searches}); "
                "request an explicit continuation."
            )
            self.notices.append(notice)
            return notice
        self.request["searches"] = count + 1
        self.persist()
        try:
            await _report_progress(self.progress, f"🔎 Searching the web for {query[:180]}…")
            return await self.research.search(query)
        except Exception:
            log.exception("Web search failed")
            return "Web search failed; do not invent evidence."

    async def fetch_page(self, url: str) -> str:
        urls = re.findall(r"https?://[^\s<>|]+", self.request["text"])
        if url not in urls or self.request.get("page_read"):
            return "Read only the supplied page once; do not follow other links."
        self.request.update(imported=True, page_read=True)
        self.persist()
        try:
            await _report_progress(self.progress, "🌐 Reading the supplied page…")
            if self.research is None:
                raise ValueError("Page retrieval is not configured")
            text = await self.research.fetch_page(url)
            notice = ""
            if len(text) > 64000:
                notice = (
                    " [Page text limited to 64000 characters; request continuation if needed.]"
                )
                self.notices.append(notice.strip())
            return (
                f"UNTRUSTED PAGE DATA (extract at most {self.max_titles} titles){notice}:\n"
                + text[:64000]
            )
        except Exception:
            self.request["import_failed"] = True
            self.persist()
            log.exception("URL extraction failed")
            return "The supplied page could not be read. Ask for an accessible URL."


Planner = Callable[[list[dict], RequestTools], Awaitable[Reply]]


async def _report_progress(
    callback: Callable[[str], Awaitable[None]] | None, text: str
) -> None:
    if callback is not None:
        try:
            await callback(text)
        except Exception:
            log.exception("Could not report conversation progress")


class Conversation:
    def __init__(
        self,
        runtime: Any,
        db_path: str,
        planner: Planner,
        *,
        research: Any = None,
        max_searches: int = 3,
        max_titles: int = 25,
    ):
        self.runtime, self.db_path, self.planner = runtime, db_path, planner
        self.lock = asyncio.Lock()
        self.research, self.max_searches, self.max_titles = research, max_searches, max_titles
        with closing(sqlite3.connect(db_path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversations (key TEXT PRIMARY KEY, data TEXT)"
            )

    def _load(self, key: str) -> dict:
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute("SELECT data FROM conversations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save(self, key: str, data: dict) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT OR REPLACE INTO conversations VALUES (?, ?)", (key, json.dumps(data))
            )
            db.commit()

    def _save_exchange(self, key: str, state: dict, text: str, response: str) -> None:
        """Append one exchange without changing this path's references or approval state."""
        state.setdefault("messages", []).extend(
            [{"role": "user", "content": text}, {"role": "assistant", "content": response}]
        )
        self._save(key, state)

    def identity_confirmations(self, event: dict, team_id: str | None) -> list[dict]:
        """Return persisted matches for this request, for Slack's confirmation cards."""
        team = team_id or event.get("team")
        if not team:
            return []
        key = f"request:{team}:{event['channel']}:{event['ts']}"
        request = self._load(key)
        if not request.get("actions"):
            thread = event.get("thread_ts") or event["ts"]
            state = self._load(f"thread:{team}:{event['channel']}:{thread}")
            key = state.get("identity_request", "")
            request = self._load(key) if key else {}
        return [
            confirmation | {"request_key": key}
            for action in request.get("actions", [])
            if (confirmation := action.get("result", {}).get("identity_confirmation"))
            and confirmation.get("status") in {"pending", "executing"}
            and (
                confirmation["status"] == "executing" or confirmation["expires"] > time.time()
            )
        ]

    async def decide_identity(
        self,
        request_key: str,
        token: str,
        event: dict,
        team_id: str | None,
        *,
        confirmed: bool,
    ) -> str | None:
        """Confirm a legacy identity, bound to its original thread and Entry."""
        if not team_id:
            return None
        async with self.lock:
            request = self._load(request_key)
            thread_key = f"thread:{team_id}:{event['channel']}:{event['thread_ts']}"
            if request.get("thread_key") != thread_key:
                return "This confirmation belongs to another thread."
            action = next(
                (
                    a
                    for a in request.get("actions", [])
                    if a.get("result", {}).get("identity_confirmation", {}).get("token")
                    == token
                ),
                None,
            )
            if action is None:
                return "This confirmation is invalid or no longer available."
            confirmation = action["result"]["identity_confirmation"]
            if confirmation.get("status") in {"confirmed", "denied"}:
                return confirmation["response"]
            if confirmation.get("status") not in {"pending", "executing"} or (
                confirmation["status"] == "pending" and confirmation["expires"] <= time.time()
            ):
                return (
                    "This confirmation expired. Ask to add the title again for a fresh match."
                )
            if not confirmed:
                if confirmation["status"] == "executing":
                    return "The identity confirmation is already being applied."
                confirmation["status"] = "denied"
                result = {
                    "status": "unchanged",
                    "title": confirmation["existing_title"],
                    "page_id": confirmation["page_id"],
                    "reason": "Match rejected. The existing Entry was left unchanged; "
                    "no duplicate was added.",
                }
            else:
                confirmation["status"] = "executing"
                self._save(request_key, request)
                try:
                    result = await self.runtime.confirm_legacy_identity(
                        confirmation["page_id"],
                        confirmation["identity"]["imdb_id"],
                        confirmation["existing_title"],
                    )
                except ValueError as exc:
                    confirmation["status"] = "invalid"
                    self._save(request_key, request)
                    return str(exc)
                except Exception:
                    log.exception("Could not confirm legacy identity")
                    return (
                        "Could not save this IMDb match. "
                        "Retry the confirmation to check its outcome."
                    )
                confirmation["status"] = "confirmed"
            confirmation["response"] = result["reason"]
            action["result"] = result | {"identity_confirmation": confirmation}
            response = await self._execute(request_key, request)
            state = self._load(thread_key)
            state["references"] = request.get("references", [])
            state.setdefault("messages", []).append({"role": "assistant", "content": response})
            self._save(thread_key, state)
            return response

    async def recover(self) -> list[dict]:
        """Resume durable executions; pending previews still need their user's approval."""
        replies = []
        async with self.lock:
            with closing(sqlite3.connect(self.db_path)) as db:
                rows = db.execute(
                    "SELECT key, data FROM conversations WHERE key LIKE 'request:%'"
                ).fetchall()
            for key, data in rows:
                request = json.loads(data)
                if request.get("status") != "executing":
                    continue
                response = await self._execute(key, request)
                thread_key = request.get("thread_key")
                if thread_key:
                    state = self._load(thread_key)
                    state.pop("pending", None)
                    state["references"] = request.get("references", [])
                    state["identity_request"] = key
                    self._save_exchange(thread_key, state, request["text"], response)
                replies.append(
                    {
                        "channel": request["channel"],
                        "thread_ts": request["thread_ts"],
                        "text": response,
                        "identity_confirmations": [
                            c | {"request_key": key}
                            for a in request["actions"]
                            if (c := a.get("result", {}).get("identity_confirmation"))
                            and c.get("status") in {"pending", "executing"}
                        ],
                    }
                )
        return replies

    async def handle(
        self,
        event: dict,
        team_id: str | None = None,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        if event.get("bot_id") or event.get("subtype"):
            return None
        if not all(event.get(k) for k in ("channel", "ts", "text")):
            return None
        team = team_id or event.get("team")
        if not team:
            return None
        thread = event.get("thread_ts") or event["ts"]
        key = f"thread:{team}:{event['channel']}:{thread}"
        async with self.lock:
            state = self._load(key)
            mentioned = bool(re.match(r"^\s*<@[A-Z0-9]+>", event["text"]))
            if not state and not mentioned:
                return None
            text = re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", event["text"]).strip()
            request_key = f"request:{team}:{event['channel']}:{event['ts']}"
            previous = self._load(request_key)
            if previous.get("response"):
                return previous["response"]
            if previous.get("status") == "pending":
                return self._preview(previous)
            if previous.get("status") == "executing":
                response = await self._execute(request_key, previous, progress=progress)
                state["references"] = previous.get("references", [])
                self._save_exchange(key, state, text, response)
                return response
            denial = re.fullmatch(r"deny\s+([0-9a-f]{16})", text)
            if denial:
                pending_key = state.get("pending")
                pending = self._load(pending_key) if pending_key else {}
                if (
                    pending.get("token") != denial[1]
                    or pending.get("status") != "pending"
                    or pending.get("expires", 0) <= time.time()
                ):
                    return (
                        "This plan is expired, already used, or no longer awaiting approval."
                    )
                response = "Denied. None of the proposed changes were applied."
                pending.update(status="denied", response=response)
                assert isinstance(pending_key, str)
                self._save(pending_key, pending)
                state.pop("pending", None)
                self._save_exchange(key, state, text, response)
                self._save(request_key, {"response": response})
                return response
            approval = re.fullmatch(r"approve\s+([0-9a-f]{16})(?:\s+([\d, ]+))?", text)
            if approval:
                pending_key = state.get("pending")
                pending = self._load(pending_key) if pending_key else {}
                if (
                    pending.get("token") != approval[1]
                    or pending.get("status") not in {"pending", "executing"}
                    or (
                        pending.get("status") == "pending"
                        and pending.get("expires", 0) <= time.time()
                    )
                ):
                    return (
                        "Approval is invalid, expired, or already used. Request a new preview."
                    )
                if approval[2]:
                    if pending.get("status") == "executing":
                        return (
                            "Execution has already started; "
                            "its approved selection cannot change."
                        )
                    selected = {int(n) for n in re.findall(r"\d+", approval[2])}
                    if (
                        not selected
                        or min(selected) < 1
                        or max(selected) > len(pending["actions"])
                    ):
                        return "Choose only the numbered actions in this preview."
                    pending["actions"] = [
                        a for i, a in enumerate(pending["actions"], 1) if i in selected
                    ]
                pending["status"] = "executing"
                assert isinstance(pending_key, str)
                self._save(pending_key, pending)
                response = await self._execute(pending_key, pending, progress=progress)
                state.pop("pending", None)
                state["references"] = pending.get("references", [])
                state["identity_request"] = pending_key
                self._save_exchange(key, state, text, response)
                self._save(request_key, {"response": response})
                return response
            messages = state.get("messages", []) + [{"role": "user", "content": text}]
            request = previous or {
                "text": text,
                "channel": event["channel"],
                "user": event.get("user", ""),
                "thread_ts": thread,
                "key": request_key,
                "thread_key": key,
                "status": "planning",
            }
            context = RequestTools(
                self.runtime,
                request,
                state.get("references", []),
                research=self.research,
                max_searches=self.max_searches,
                max_titles=self.max_titles,
                persist=lambda: self._save(request_key, request),
                progress=progress,
            )
            try:
                reply = await self.planner(messages, context)
                await _report_progress(progress, "📝 Checking the result…")
                available = {
                    e.page_id for e in await self.runtime.list_watchlist() if not e.removed
                }
                if any(r not in available for r in reply.references):
                    raise ValueError("Displayed references must match available Entries")
                actions = await self._prepare(context)
            except Exception:
                log.exception("Conversation planning failed")
                return (
                    "I couldn't safely prepare that request. "
                    "Nothing was changed; please clarify or retry."
                )
            request.update(actions=actions, status="executing")
            affected_entries = {
                action.get("imdb_id") or action.get("page_id") or str(index)
                for index, action in enumerate(actions)
            }
            if actions and (
                re.search(r"https?://", text)
                or request.get("imported")
                or not all(self._explicit_action(text, a["operation"]) for a in actions)
                or len(affected_entries) >= 4
            ):
                request.update(
                    status="pending", token=secrets.token_hex(8), expires=time.time() + 1800
                )
                state["pending"] = request_key
                response = self._preview(request)
                self._save(request_key, request)
            elif actions:
                self._save(request_key, request)
                response = await self._execute(request_key, request, progress=progress)
                state.pop("pending", None)
            else:
                request["status"] = "answered"
                response = reply.text
            if context.notices:
                response += "\n" + "\n".join(dict.fromkeys(context.notices))
            if request.get("status") == "done":
                references = request.get("references", [])
            else:
                references = reply.references or state.get("references", [])
            state.update(
                identity_request=request_key,
                messages=messages + [{"role": "assistant", "content": response}],
                references=references,
            )
            self._save(key, state)
            if request.get("status") != "pending":
                self._save(request_key, request | {"response": response})
            return response

    async def _prepare(self, context: RequestTools) -> list[dict]:
        entries = {e.page_id: e for e in await self.runtime.list_watchlist()}
        actions: list[dict] = []
        seen = set()
        for index, action in enumerate(context.actions):
            prepared = action.model_dump()
            reason = None
            if action.operation == "add":
                prepared["page_id"] = None
                if not action.imdb_id or not re.fullmatch(r"tt\d{7,12}", action.imdb_id):
                    reason = (
                        "Resolve the requested title to a valid IMDb Identity before adding."
                    )
                else:
                    try:
                        prepared.update(await self.runtime.validate_identity(action.imdb_id))
                    except Exception:
                        log.exception("Could not verify IMDb Identity for staged Add")
                        reason = (
                            "Could not verify this IMDb Identity; confirm the title and retry."
                        )
                if reason:
                    prepared.setdefault("title", action.imdb_id or "Unresolved Add")
            else:
                page_id = action.page_id
                if action.reference is not None:
                    if action.reference > len(context.references):
                        reason = "That numbered Entry was not displayed in this thread."
                    else:
                        page_id = context.references[action.reference - 1]
                entry = entries.get(page_id)
                if reason is None and (entry is None or entry.removed):
                    reason = (
                        "That Entry is no longer in the Watchlist; choose a current Entry."
                    )
                elif reason is None and entry is not None:
                    prepared.update(
                        page_id=entry.page_id, title=entry.title, imdb_id=entry.imdb_id
                    )
                if reason:
                    prepared["page_id"] = page_id
                    prepared.setdefault(
                        "title",
                        f"Entry {page_id}"
                        if page_id
                        else f"Entry reference {action.reference}",
                    )
            if reason:
                prepared["result"] = {
                    "title": prepared["title"],
                    "status": "clarification_required",
                    "reason": reason,
                }
            identity = prepared.get("imdb_id") or prepared.get("page_id")
            marker = (
                action.operation,
                identity if identity is not None else f"clarification:{index}",
            )
            if marker not in seen:
                seen.add(marker)
                actions.append(prepared)
        return actions

    @staticmethod
    def _explicit_action(text: str, operation: str) -> bool:
        """Ambiguous prose gets a preview; retrieved content never supplies permission."""
        command, polite = re.subn(
            r"^\s*(?:can|could|would) you\s+(?:please\s+)?"
            r"(?=(?:add|put|include|save|remove|delete|trash|mark|set|flag|refresh|re-enrich)\b)",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if polite:
            text = command.replace("?", "", 1)
        if re.search(
            r"\?|\b(?:don't|do not|never|not|can't|cannot|could|would|should|might|"
            r"maybe|perhaps|whether|if)\b",
            text,
            re.IGNORECASE,
        ):
            return False
        if re.match(
            r"\s*(?:can you\s+)?"
            r"(?:what|which|recommend|suggest|find|give|show|tell|list|search)\b",
            text,
            re.IGNORECASE,
        ):
            return False
        patterns = {
            "add": r"\b(?:add|put|include|save)\b",
            "remove": r"\b(?:remove|delete|trash)\b",
            "watched": (
                r"\b(?:mark|set|flag)\b.*\bwatched\b"
                r"|\bi (?:have |already |just )?watched\b"
            ),
            "unwatched": r"\b(?:mark|set|flag)\b.*\bunwatched\b",
            "refresh": r"\b(?:refresh|re-enrich)\b",
        }
        return bool(re.search(patterns[operation], text, re.IGNORECASE))

    @staticmethod
    def _preview(request: dict) -> str:
        lines = [
            f"{i}. {a['operation']}: {a['title']} "
            f"({a.get('imdb_id') or a.get('page_id') or 'unresolved'})"
            for i, a in enumerate(request["actions"], 1)
        ]
        return (
            "Approve this exact action list (expires in 30 minutes):\n"
            + "\n".join(lines)
            + f"\nReply `approve {request['token']}` or select numbers, e.g. "
            + f"`approve {request['token']} 1,2`."
        )

    async def _execute(
        self,
        key: str,
        request: dict,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        results = []
        for i, action in enumerate(request["actions"]):
            if action.get("result"):
                result = action["result"]
            elif action.get("started"):
                # A crash may have occurred after the external effect but before its receipt.
                # Reconcile by identity/state and never blindly repeat an uncertain write.
                current = await self.runtime.list_watchlist()
                entry = next(
                    (
                        e
                        for e in current
                        if (action["operation"] == "add" and e.imdb_id == action["imdb_id"])
                        or e.page_id == action.get("page_id")
                    ),
                    None,
                )
                applied = entry is not None and (
                    action["operation"] == "add"
                    or (action["operation"] == "watched" and entry.watched is True)
                    or (action["operation"] == "unwatched" and entry.watched is False)
                )
                result = {
                    "title": action["title"],
                    "status": "recovered" if applied else "uncertain",
                    "page_id": entry.page_id if entry else action.get("page_id"),
                }
                action["result"] = result
                self._save(key, request)
            else:
                await _report_progress(
                    progress, f"🎬 Applying the requested change to {action['title']}…"
                )
                action["started"] = True
                self._save(key, request)
                try:
                    arguments = {k: request[k] for k in ("channel", "user", "thread_ts")}
                    arguments.update(
                        request_key=f"{key}:{i}", original_request=request["text"]
                    )
                    if action["operation"] == "add":
                        result = await self.runtime.add_resolved(
                            action["imdb_id"], **arguments
                        )
                    else:
                        result = await self.runtime.mutate_entry(
                            action["page_id"], action["operation"], **arguments
                        )
                except Exception:
                    log.exception("Watchlist action failed for request %s", key)
                    result = {"title": action["title"], "status": "failed"}
                action["result"] = result
                self._save(key, request)
            confirmation = result.get("identity_confirmation")
            if confirmation and not confirmation.get("token"):
                confirmation.update(
                    token=secrets.token_hex(8), expires=time.time() + 1800, status="pending"
                )
                self._save(key, request)
            if confirmation and confirmation.get("status") in {"pending", "executing"}:
                results.append(
                    f"{i + 1}. {result.get('title') or action['title']}: "
                    "Please confirm the existing Entry's IMDb match below."
                )
                continue
            results.append(
                f"{i + 1}. {result.get('title') or action['title']}: {result['status']}"
                + (f" — {result['reason']}" if result.get("reason") else "")
            )
        request["references"] = [
            a["result"].get("page_id") or a.get("page_id") for a in request["actions"]
        ]
        request["status"] = "done"
        response = "\n".join(results)
        request["response"] = response
        self._save(key, request)
        return response

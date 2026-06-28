"""Spike 03 — AsyncSqliteSaver + interrupt() SURVIVES a process kill/restart.

The core learning target and biggest risk (ADR 0006/0007): an `interrupt()` must persist
to a `.sqlite` file so a human can answer minutes/days later — even if the process died in
between. This spike proves the full lifecycle across TWO separate OS processes sharing one
`.sqlite` file:

    step 1 (process A):  invoke -> interrupt() -> ainvoke() RETURNS -> process EXITS
    step 2 (process B):  fresh process, reopen the SAME .sqlite, resume -> completes

If durability worked, process B knows nothing except the thread_id + the resume value, yet
the graph picks up exactly where process A paused. That is durable execution.

Also confirms (ADR 0006 depends on these):
  - interrupt() makes ainvoke() RETURN (so reconcile can "move on" to the next Title)
  - the saver's WAL journal mode + .setup() lifecycle

Credential-free. Run the two steps as separate processes (this is the whole point):

    uv run python spikes/03_sqlite_interrupt_restart.py reset
    uv run python spikes/03_sqlite_interrupt_restart.py interrupt --thread page-123
    uv run python spikes/03_sqlite_interrupt_restart.py resume    --thread page-123 --value tt1160419
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

DB_PATH = Path(__file__).resolve().parent / "_out" / "spike03_checkpoints.sqlite"


class State(TypedDict):
    title: str
    chosen: str | None


def resolve(state: State) -> dict:
    return {"title": state["title"], "chosen": None}


def disambiguate(state: State) -> dict:
    # Pause here. ainvoke() returns to the caller; the pending checkpoint is in sqlite.
    chosen = interrupt({"title": state["title"], "candidates": ["tt1160419", "tt0087182"]})
    return {"chosen": chosen}


def finalize(state: State) -> dict:
    return {"chosen": state["chosen"]}


def build():
    g = StateGraph(State)
    g.add_node("resolve", resolve)
    g.add_node("disambiguate", disambiguate)
    g.add_node("finalize", finalize)
    g.add_edge(START, "resolve")
    g.add_edge("resolve", "disambiguate")
    g.add_edge("disambiguate", "finalize")
    g.add_edge("finalize", END)
    return g


async def step_interrupt(thread: str) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(DB_PATH)) as saver:
        await saver.setup()  # idempotent: CREATE TABLE IF NOT EXISTS; sets WAL
        graph = build().compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": thread}}
        result = await graph.ainvoke({"title": "Dune", "chosen": None}, cfg)
        assert "__interrupt__" in result, f"expected interrupt, got {list(result)}"
        payload = result["__interrupt__"][0].value
        print(f"[process A] ainvoke() RETURNED on interrupt for thread={thread!r}")
        print(f"[process A] surfaced payload: {payload}")
        print(f"[process A] checkpoint persisted to {DB_PATH.name}; now EXITING (simulated kill)")
    await _print_journal_mode()


async def step_resume(thread: str, value: str) -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"no checkpoint db at {DB_PATH} — run the `interrupt` step first")
    # Brand-new process / brand-new connection. We know only the thread_id + resume value.
    async with AsyncSqliteSaver.from_conn_string(str(DB_PATH)) as saver:
        await saver.setup()
        graph = build().compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": thread}}
        snap = await graph.aget_state(cfg)
        print(f"[process B] reopened {DB_PATH.name}; next node(s) pending = {snap.next}")
        assert snap.next, "no pending state found — durability FAILED (nothing to resume)"
        out = await graph.ainvoke(Command(resume=value), cfg)
        print(f"[process B] resumed with Command(resume={value!r}); chosen = {out['chosen']}")
        assert out["chosen"] == value, "resume value did not flow through"
        print("[process B] graph COMPLETED across a process restart ✓")


async def _print_journal_mode() -> None:
    import aiosqlite

    async with aiosqlite.connect(str(DB_PATH)) as db:
        async with db.execute("PRAGMA journal_mode;") as cur:
            row = await cur.fetchone()
    print(f"[info] sqlite journal_mode = {row[0]!r} (WAL expected for concurrent readers)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset")
    pi = sub.add_parser("interrupt")
    pi.add_argument("--thread", required=True)
    pr = sub.add_parser("resume")
    pr.add_argument("--thread", required=True)
    pr.add_argument("--value", required=True)
    args = p.parse_args()

    if args.cmd == "reset":
        for suffix in ("", "-wal", "-shm"):
            f = DB_PATH.with_name(DB_PATH.name + suffix)
            if f.exists():
                f.unlink()
                print(f"removed {f.name}")
        print("clean.")
    elif args.cmd == "interrupt":
        asyncio.run(step_interrupt(args.thread))
    elif args.cmd == "resume":
        asyncio.run(step_resume(args.thread, args.value))


if __name__ == "__main__":
    main()

"""Spike 02 — LangGraph 1.x toy graph: fan-out, reducer fan-in, conditional edge, interrupt().

De-risks the core graph primitives every later phase leans on, *against the installed
langgraph 1.2.6* (not tutorials). Proves, in one runnable graph:

  - parallel fan-out          : dispatch -> {lane_omdb, lane_rt}  (run concurrently)
  - reducer fan-in            : both lanes append to one Annotated[list, add] channel;
                                `assemble` runs once, after BOTH (superstep barrier)
  - add_conditional_edges     : assemble routes on a function's return -> finish | disambiguate
  - interrupt()               : disambiguate pauses; invoke() RETURNS with an __interrupt__;
                                Command(resume=...) finishes it

It also pins the *exact* signatures (StateGraph / RetryPolicy / Command) we'll reuse —
see the SIGNATURES block printed at the end.

Credential-free. Run:  uv run python spikes/02_langgraph_toy_graph.py
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt


# --- State ----------------------------------------------------------------------------
# `lane_results` uses a reducer (operator.add on lists): when lane_omdb and lane_rt both
# write concurrently in the same superstep, LangGraph MERGES their updates instead of
# raising InvalidUpdateError. This is the fan-in mechanism (mirrors OMDb ‖ RT assemble).
class State(TypedDict):
    title: str
    lane_results: Annotated[list[str], operator.add]
    ambiguous: bool
    chosen: str | None


# --- Nodes ----------------------------------------------------------------------------
def dispatch(state: State) -> dict:
    # Fan-out point. Returns nothing structural; the two outgoing edges do the fan-out.
    return {"lane_results": [f"dispatch({state['title']})"]}


def lane_omdb(state: State) -> dict:
    return {"lane_results": ["omdb:tt0000001"]}


def lane_rt(state: State) -> dict:
    return {"lane_results": ["rt:critic=91,audience=88"]}


def assemble(state: State) -> dict:
    # Runs ONCE, only after both lanes complete (fan-in barrier). Proof: lane_results
    # contains both lanes' contributions by the time we get here.
    return {}


def disambiguate(state: State) -> dict:
    # interrupt() pauses the graph and makes invoke() RETURN (the value shows up under
    # the "__interrupt__" key). The arg is the payload surfaced to the human/Slack.
    chosen_imdb_id = interrupt(
        {"question": f"Which match for {state['title']!r}?", "candidates": ["tt1", "tt2"]}
    )
    # Execution continues HERE on resume, with `chosen_imdb_id` == the Command(resume=...) value.
    return {"chosen": chosen_imdb_id}


def finish(state: State) -> dict:
    return {"chosen": state.get("chosen") or "auto"}


# --- Conditional edge ------------------------------------------------------------------
def route_after_assemble(state: State) -> str:
    # The function's return string is matched against the mapping passed to
    # add_conditional_edges. Mirrors disambiguation: confident -> proceed, unsure -> HITL.
    return "disambiguate" if state["ambiguous"] else "finish"


def build_graph():
    g = StateGraph(State)

    # RetryPolicy attaches per-node via add_node(..., retry_policy=...). Capturing the
    # signature here so Phase 7 reuses the exact field names.
    retry = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))

    g.add_node("dispatch", dispatch)
    g.add_node("lane_omdb", lane_omdb, retry_policy=retry)  # <-- per-node RetryPolicy
    g.add_node("lane_rt", lane_rt, retry_policy=retry)
    g.add_node("assemble", assemble)
    g.add_node("disambiguate", disambiguate)
    g.add_node("finish", finish)

    g.add_edge(START, "dispatch")
    # Fan-out: two edges out of one node -> both targets run in parallel.
    g.add_edge("dispatch", "lane_omdb")
    g.add_edge("dispatch", "lane_rt")
    # Fan-in: both lanes -> assemble. LangGraph waits for BOTH before running assemble.
    g.add_edge("lane_omdb", "assemble")
    g.add_edge("lane_rt", "assemble")
    # Conditional edge.
    g.add_conditional_edges(
        "assemble", route_after_assemble, {"disambiguate": "disambiguate", "finish": "finish"}
    )
    g.add_edge("disambiguate", "finish")
    g.add_edge("finish", END)

    # interrupt() requires a checkpointer. InMemorySaver here; spike 04 swaps in the
    # durable AsyncSqliteSaver and proves restart-survival.
    return g.compile(checkpointer=InMemorySaver())


def main() -> None:
    graph = build_graph()

    print("=" * 78)
    print("CASE 1 — confident path (no interrupt): fan-out ‖ -> fan-in -> finish")
    print("=" * 78)
    cfg1 = {"configurable": {"thread_id": "confident"}}
    out1 = graph.invoke({"title": "Dune (2021)", "ambiguous": False, "chosen": None}, cfg1)
    print("lane_results (fan-in merged both lanes):", out1["lane_results"])
    assert "omdb:tt0000001" in out1["lane_results"], "OMDb lane missing"
    assert "rt:critic=91,audience=88" in out1["lane_results"], "RT lane missing"
    assert out1["chosen"] == "auto"
    print("-> finished without interrupt; chosen =", out1["chosen"])

    print()
    print("=" * 78)
    print("CASE 2 — ambiguous path: conditional edge -> disambiguate -> interrupt()")
    print("=" * 78)
    cfg2 = {"configurable": {"thread_id": "ambiguous"}}
    paused = graph.invoke({"title": "Dune", "ambiguous": True, "chosen": None}, cfg2)
    # interrupt() makes invoke() RETURN; the payload is under "__interrupt__".
    assert "__interrupt__" in paused, f"expected interrupt, got keys {list(paused)}"
    intr = paused["__interrupt__"][0]
    print("invoke() RETURNED on interrupt. Payload:", intr.value)

    # Out-of-band resume (mirrors a Slack click) — Command(resume=<value>).
    resumed = graph.invoke(Command(resume="tt2"), cfg2)
    print("resumed with Command(resume='tt2'); chosen =", resumed["chosen"])
    assert resumed["chosen"] == "tt2", "resume value did not flow through"

    print()
    print("=" * 78)
    print("SIGNATURES (verified against installed versions)")
    print("=" * 78)
    print("StateGraph(State)  ->  .add_node(name, fn, retry_policy=RetryPolicy(...))")
    print("                       .add_edge(START|name, name)   # parallel: 2+ edges out")
    print("                       .add_conditional_edges(src, router_fn, {ret: dest})")
    print("                       .compile(checkpointer=InMemorySaver())")
    print("RetryPolicy fields :", RetryPolicy._fields)  # NamedTuple in 1.x
    print("interrupt(payload) -> invoke() returns {'__interrupt__': (Interrupt(value=...),)}")
    print("Command(resume=v)  -> graph.invoke(Command(resume=v), config) continues the thread")
    print("\nALL ASSERTIONS PASSED ✓")


if __name__ == "__main__":
    main()

"""Transient-error retry policy — the shared "what is worth retrying" contract (Phase 7).

One definition of *transient* (`is_transient`), consumed by two layers that must agree:

- the graph's node-level `RetryPolicy` (`transient_retry_policy`), applied to the **gating**
  nodes (OMDb + Notion) whose exhausted-transient *should* re-raise → leave the Entry `pending`
  → the cron retries it (ADR 0004); and
- the clients' own bounded retry loops (`FirecrawlClient._search`, `NotionClient._request`),
  which sit **below** a best-effort swallow so a transient blip is retried *within* the
  provider rather than fed up to a node that would fail the Entry.

Transient = a failure a plain retry can plausibly fix: a network/timeout error, or a 429 /
5xx from the server. Deterministic errors (`ValueError`, a pydantic `ValidationError`, a 4xx,
a `KeyError`) are **not** retried — retrying them just burns quota to fail identically. ADR
0013.
"""

from __future__ import annotations

import httpx
from langgraph.types import RetryPolicy

# HTTP statuses worth retrying: rate-limit + the transient 5xx family. Shared with the clients
# so their status checks match the RetryPolicy predicate exactly.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """True if `exc` is a transient failure a retry can plausibly fix (429 / 5xx / transport).

    `httpx.TransportError` covers the connect/read/write/pool timeouts and network errors;
    `httpx.HTTPStatusError` (from `raise_for_status`) is retried only for `TRANSIENT_STATUS`.
    Everything else — `ValueError`, pydantic `ValidationError`, a 4xx, a `KeyError` — returns
    False, so deterministic failures fail fast instead of retrying to the same result.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUS
    return False


def transient_retry_policy(max_attempts: int) -> RetryPolicy:
    """A langgraph `RetryPolicy` retrying only `is_transient` failures, with backoff + jitter.

    Keeps langgraph's exp-backoff defaults (`initial_interval=0.5`, `backoff_factor=2.0`,
    `max_interval=128`); `jitter=True` spreads concurrent retries so a shared upstream 429
    doesn't get hammered in lockstep. Applied per-node in `build_graph` to the gating nodes
    only (ADR 0013).
    """
    return RetryPolicy(retry_on=is_transient, max_attempts=max_attempts, jitter=True)

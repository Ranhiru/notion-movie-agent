"""LLM factories — the OpenAI-compatible chat models behind the LLM graph nodes (ADR 0011).

ADR 0008 redistributes the `with_structured_output` work across three *roles* — extraction
(Phase 4), disambiguation (Phase 6), judge (Phase 5) — each with its own configurable model
(RESEARCH §10 / ADR 0011). All three point at one `OPENAI_BASE_URL` (a local LLM or
OpenRouter); only the `model=` differs, so they swap per-role without touching the nodes.

`temperature=0` everywhere: these are extraction/classification calls, not generation — we
want the most deterministic parse the endpoint will give. Every model must support
tool-calling, since `with_structured_output` is implemented via tool calls (proven for the
local model by `spikes/04_local_llm_structured_output.py`).
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from .config import Settings


def extraction_model(settings: Settings) -> ChatOpenAI:
    """The RT-extraction model (`OPENAI_EXTRACTION_MODEL`) — Phase 4's first LLM node.

    Bound once at startup and reused across entries; `ChatOpenAI` is stateless per `.invoke`.
    Phase 5 (`judge_model`) and Phase 6 (`disambiguation_model`) add sibling factories that
    differ only in which `OPENAI_*_MODEL` they read.
    """
    return ChatOpenAI(
        base_url=settings.OPENAI_BASE_URL,
        api_key=settings.OPENAI_API_KEY,  # type: ignore[arg-type]  # str coerced to SecretStr
        model=settings.OPENAI_EXTRACTION_MODEL,
        temperature=0,
        # Bound the response so a reasoning model can't emit unbounded <think> and hang the
        # sweep. `max_completion_tokens` is the ChatOpenAI field alias for `max_tokens`; 0 in
        # config → None → omit the cap (server default). ADR 0011.
        max_completion_tokens=settings.OPENAI_MAX_TOKENS or None,
    )


def judge_model(settings: Settings) -> ChatOpenAI:
    """The judge model (`OPENAI_JUDGE_MODEL`) — Phase 5's LLM-as-judge (ADR 0008, role 3).

    Also drives Phase 5's `resolve_rt` correlation (same identity-judgment role family). A
    sibling of `extraction_model`, differing only in the configured model; a stronger model
    than extraction is the intended split (RESEARCH §5), swapped via config, not code.
    """
    return ChatOpenAI(
        base_url=settings.OPENAI_BASE_URL,
        api_key=settings.OPENAI_API_KEY,  # type: ignore[arg-type]  # str coerced to SecretStr
        model=settings.OPENAI_JUDGE_MODEL,
        temperature=0,
        max_completion_tokens=settings.OPENAI_MAX_TOKENS or None,  # see extraction_model
    )


def disambiguation_model(settings: Settings) -> ChatOpenAI:
    """The disambiguation pre-filter model (`OPENAI_DISAMBIGUATION_MODEL`) — Phase 6a, role 2.

    Picks the best OMDb candidate when search returns >1 and self-reports whether the pick is
    a clear match (ADR 0008 role 2). Third and final of the three ADR-0008 role factories; a
    sibling of `extraction_model` / `judge_model`, differing only in the configured model.
    """
    return ChatOpenAI(
        base_url=settings.OPENAI_BASE_URL,
        api_key=settings.OPENAI_API_KEY,  # type: ignore[arg-type]  # str coerced to SecretStr
        model=settings.OPENAI_DISAMBIGUATION_MODEL,
        temperature=0,
        max_completion_tokens=settings.OPENAI_MAX_TOKENS or None,  # see extraction_model
    )

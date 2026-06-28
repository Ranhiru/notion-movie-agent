"""Spike 04 — local-LLM `with_structured_output` reliability (MANDATORY pre-req).

Every LLM node (RT extraction, disambiguation, judge) uses `with_structured_output`, which
relies on tool-calling. Local / OpenRouter models vary wildly in how well they honor that.
This spike runs all three role-models through `with_structured_output` and confirms each
returns a *clean Pydantic object* (ADR 0008/0011). If one is unreliable, we learn it NOW —
the fallback is OpenRouter with a known tool-calling model (two env vars).

Needs: OPENAI_BASE_URL, OPENAI_API_KEY, and the three OPENAI_*_MODEL values.

Run:  uv run python spikes/04_local_llm_structured_output.py
"""

from __future__ import annotations

from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

import _env


# Schemas mirror the three real LLM jobs so the spike tests representative shapes.
class RTScores(BaseModel):
    """RT extraction node output."""

    rt_critic: int | None = Field(None, ge=0, le=100, description="Tomatometer 0-100")
    rt_audience: int | None = Field(None, ge=0, le=100, description="Popcornmeter 0-100")


class Pick(BaseModel):
    """Disambiguation pre-filter output."""

    imdb_id: str = Field(description="chosen candidate's imdbID")
    confident: bool


class Verdict(BaseModel):
    """LLM-as-judge output."""

    confidence: Literal["high", "medium", "low"]
    wrong_match: bool


JOBS = [
    (
        "OPENAI_EXTRACTION_MODEL",
        RTScores,
        "Extract the Rotten Tomatoes scores. The Tomatometer is 92% and the "
        "Popcornmeter is 87%.",
    ),
    (
        "OPENAI_DISAMBIGUATION_MODEL",
        Pick,
        "OMDb returned: [Dune (2021) imdbID tt1160419], [Dune (1984) imdbID tt0087182]. "
        "The user typed 'Dune' and tagged it Movie, added in 2024. Pick the best match.",
    ),
    (
        "OPENAI_JUDGE_MODEL",
        Verdict,
        "Record: title 'Dune: Part Two', IMDb 8.5, RT critic 92, RT audience 90, plot about "
        "Paul Atreides. Do the numbers and plot agree (no wrong-match anomaly)?",
    ),
]


def run_job(base_url: str, api_key: str, model: str, schema: type[BaseModel], prompt: str):
    llm = ChatOpenAI(base_url=base_url, api_key=api_key, model=model, temperature=0)
    structured = llm.with_structured_output(schema)
    return structured.invoke(prompt)


def main() -> None:
    base_url, api_key = _env.require("OPENAI_BASE_URL", "OPENAI_API_KEY")

    print(f"endpoint: {base_url}\n")
    all_ok = True
    for env_name, schema, prompt in JOBS:
        model = _env.get(env_name)
        if not model:
            print(f"✗ {env_name} unset — skipping ({schema.__name__})")
            all_ok = False
            continue
        try:
            result = run_job(base_url, api_key, model, schema, prompt)
            assert isinstance(result, schema), f"got {type(result)}, want {schema}"
            print(f"✓ {env_name} = {model!r}")
            print(f"    -> {schema.__name__}: {result.model_dump()}")
        except Exception as e:  # noqa: BLE001 — spike: report any failure plainly
            all_ok = False
            print(f"✗ {env_name} = {model!r} FAILED structured output:")
            print(f"    {type(e).__name__}: {e}")
            print("    (if this model can't tool-call, fall back to OpenRouter — ADR 0011)")
        print()

    if all_ok:
        print("structured-output spike OK ✓  all three role-models parse cleanly")
    else:
        print("structured-output spike: SOME MODELS FAILED — see above (surface now, per Phase 0)")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

# LLM endpoint: configurable OpenAI-compatible base URL, three configurable models

All LLM nodes ([0008](./0008-llm-node-architecture.md)) call an **OpenAI-compatible endpoint**
through `langchain-openai`'s `ChatOpenAI` — with the **base URL configurable** so the same code
runs against **OpenRouter** *or* a **local LLM** during development (e.g. llama.cpp / Ollama /
LM Studio on `localhost`), or any other OpenAI-compatible server. Supersedes ADR 0008's
direct-Anthropic `claude-haiku-4-5` choice.

```python
ChatOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, model=…).with_structured_output(...)
```

## Configuration (generic `OPENAI_*`, not provider-specific)

| Setting | Env var | Example |
|---|---|---|
| Endpoint | `OPENAI_BASE_URL` | `http://localhost:8888/v1` (dev) · `https://openrouter.ai/api/v1` |
| Key | `OPENAI_API_KEY` | (empty for a local server) |
| RT extraction model | `OPENAI_EXTRACTION_MODEL` | |
| Disambiguation model | `OPENAI_DISAMBIGUATION_MODEL` | |
| Judge model | `OPENAI_JUDGE_MODEL` | |

One client config (base URL + key), three model knobs — a cheap model for mechanical
extraction, a stronger one for the quality-critical judge. Endpoint and models swap via config,
not code (a core learning-project convenience).

## Constraint

Every node uses `with_structured_output`, so **each configured model must support structured
outputs / tool-calling** at the chosen endpoint. A local model without reliable tool-calling
will fail these nodes — pick accordingly, or point `OPENAI_BASE_URL` at OpenRouter for a model
that does.

## Considered Options

- **Direct Anthropic, single Haiku 4.5 (ADR 0008, superseded):** fewer moving parts, but
  provider lock, no local-model dev, no per-role tuning.
- **Hardcoded OpenRouter base URL (rejected):** blocks local-LLM development.
- **Configurable OpenAI-compatible base URL + one model per role (chosen).**

## Consequences

- Develop against a free local model; switch to OpenRouter by changing two env vars.
- Three model knobs in `.env`; experimentation is a config change, not a code change.

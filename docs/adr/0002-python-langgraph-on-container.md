# Python + LangGraph on a single long-lived container (not Cloudflare)

The service is a single long-lived Python process — FastAPI webhook + in-process
scheduler + the LangGraph workflow — packaged as a Docker container so it runs
identically on a laptop and on a container host (Fly.io / Railway / Render). The
single-flight lock is therefore an in-process `asyncio.Lock`; no external/global lock
is needed.

## Considered Options

- **Cloudflare Workers (rejected).** Two collisions with the project's stated #1 goal,
  *learn LangGraph properly*: (1) Workers can't run a long background job — `waitUntil`
  caps background work at 30s, and Cron/Queue/DO-Alarm at 15 min, while the ~100-item
  backfill is 10+ min and Firecrawl-bound; Cloudflare's durable-job answer is
  **Workflows**, which is *itself* a durable-execution engine that overlaps and makes
  redundant LangGraph's checkpointer (learning target #3). (2) Python on Workers is
  Pyodide/beta and does not list `langgraph` as a supported package.
- **Cloudflare hybrid (Worker proxy → container) (rejected):** the Worker would be a
  thin proxy, adding moving parts over pointing Notion straight at the container.
- **Python container (chosen).**

## Consequences

- LangGraph's durable execution / checkpointer is the thing we learn, not the thing we
  fight.
- Choosing one long-lived process is what makes the in-process lock correct; it also
  rules out ephemeral/serverless hosts (Lambda, Workers) by design.
- A process restart mid-reconcile interrupts the in-flight sweep; idempotent writes +
  the checkpointer + the next cron recover it.

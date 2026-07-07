# Phase 10 (ADR 0009) — the one Docker phase. The image is a long-lived process that runs
# `python -m notion_db_updater --serve` (in-process cron + Slack Socket Mode). No inbound HTTP,
# so no ports are exposed; Slack is an *outbound* WebSocket (Socket Mode).
#
# Multi-stage, uv-based, reproducible from uv.lock (`--frozen` = fail if the lock is stale).
# Stage 1 installs deps into a venv; stage 2 is a slim runtime that copies the venv + source.

# --- Stage 1: build the venv from the pinned lockfile -------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# Bytecode-compile on install (faster cold start) and copy (not symlink) into the venv so it
# survives being moved to the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dep layer first (cached until the lockfile changes): install *only* the locked runtime deps,
# without the project itself or the dev group, so editing src/ doesn't bust the dep cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now install the project itself against that cached dep layer.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2: slim runtime ----------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

# Run as a non-root user. Create /data (the checkpointer volume mount point) owned by that
# user *in the image* — Docker seeds a fresh named volume from the image path's contents and
# ownership on first mount, so the AsyncSqliteSaver can write checkpoints.sqlite there.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir /data \
    && chown appuser:appuser /data

WORKDIR /app

# The venv from the builder + the source. Put the venv's bin first on PATH so `python` is the
# project interpreter with all deps (no `uv run` at runtime → no network / no sync on boot).
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser

# The checkpointer lives on the named volume (ADR 0007 — must be persistent). Overridden by
# compose env, but defaulted here so `docker run` alone still writes to the volume mount.
ENV CHECKPOINT_DB_PATH=/data/checkpoints.sqlite
VOLUME ["/data"]

# entrypoint.sh bridges file-mounted compose secrets (/run/secrets/*) into env vars, then
# exec's the serve process (see the script for why — LangSmith reads os.environ directly).
ENTRYPOINT ["./entrypoint.sh"]

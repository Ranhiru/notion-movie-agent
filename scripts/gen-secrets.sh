#!/bin/sh
# Phase 10 (ADR 0009) — project the secret keys out of .env into per-file secrets/ files that
# docker-compose.yml mounts as file-based secrets. `.env` stays the single source of truth for
# dev; run this (or `make secrets`) after editing/rotating any key, then `docker compose up`.
#
# Writes one file per secret with printf '%s' (no trailing newline). The whole secrets/ dir is
# gitignored — real values never leave your machine.
set -e

ENV_FILE="${1:-.env}"
OUT_DIR="secrets"

# The nine sensitive keys (everything else in .env is non-secret config → compose environment:).
SECRETS="NOTION_MOVIE_DB_TOKEN OMDB_API_KEY FIRECRAWL_API_KEY TAVILY_API_KEY EXA_API_KEY \
OPENAI_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN LANGSMITH_API_KEY"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found (pass a path as \$1, or run from the project root)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

missing=""
for key in $SECRETS; do
  # First matching `KEY=...` line; strip only the `KEY=` prefix (values may contain `=`).
  val=$(grep "^$key=" "$ENV_FILE" | head -1 | cut -d= -f2-)
  if [ -z "$val" ]; then
    missing="$missing $key"
  fi
  printf '%s' "$val" > "$OUT_DIR/$key"
  chmod 600 "$OUT_DIR/$key"
  echo "wrote $OUT_DIR/$key"
done

if [ -n "$missing" ]; then
  echo ""
  echo "warning: these keys were empty/absent in $ENV_FILE (wrote empty files):$missing" >&2
  echo "fill them in $ENV_FILE and re-run, or the container will boot without them." >&2
fi

#!/bin/sh
# Phase 10 secret bridge (ADR 0009). Docker Compose mounts each file-mounted secret at
# /run/secrets/<NAME> (a read-only tmpfs file, NOT an env var). This script reads each one and
# exports it as an env var named after the file, then exec's the app.
#
# Why export to the environment rather than let pydantic read the files directly: LangChain /
# LangSmith read LANGSMITH_* straight from os.environ at runtime (see config.py get_settings),
# bypassing the typed Settings object. Exporting reproduces exactly what load_dotenv does from
# .env in dev — one uniform mechanism that feeds both pydantic-settings AND os.environ. No app
# code changes; Phase 10 stays a pure deploy concern.
#
# The secret's NAME in docker-compose.yml must match the env var the code expects
# (NOTION_MOVIE_DB_TOKEN, OPENAI_API_KEY, LANGSMITH_API_KEY, ...) — basename below becomes the
# exported var name. `$(cat)` strips any trailing newline in the secret file.
set -e

if [ -d /run/secrets ]; then
  for f in /run/secrets/*; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    export "$name=$(cat "$f")"
  done
fi

# Non-secret config (CHECKPOINT_DB_PATH, OPENAI_BASE_URL, RPMs, LangSmith endpoint/project, ...)
# arrives via compose `environment:` and is already in the env — untouched here.
exec python -m notion_db_updater --serve

#!/bin/sh
# Phase 10 (ADR 0009) — project every key in .env into a per-file secrets/ file that
# docker-compose.yml can mount as a file-based secret. `.env` stays the single source of truth
# for dev; run this (or `make secrets`) after editing/rotating any key, then `docker compose up`.
#
# Keys are read straight from .env (no hardcoded list — add a key to .env and it's picked up).
# docker-compose.yml decides which of these files are actually mounted as secrets; the rest are
# simply unused. Each value is written with printf '%s' (no trailing newline); `$(cat)` in the
# entrypoint strips any stray newline anyway. The whole secrets/ dir is gitignored — real values
# never leave your machine.
set -e

ENV_FILE="${1:-.env}"
OUT_DIR="secrets"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found (pass a path as \$1, or run from the project root)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

count=0
missing=""
while IFS= read -r line || [ -n "$line" ]; do
  # Skip blank lines and comments (the whole line, not values — a value may contain '#').
  case "$line" in
    '' | '#'*) continue ;;
  esac
  # Only KEY=VALUE lines.
  case "$line" in
    *=*) ;;
    *) continue ;;
  esac
  key=${line%%=*}
  # Guard: KEY must be a shell/env identifier (letters, digits, underscore; no leading digit).
  # This also rejects any accidental leading whitespace or `export ` prefix.
  case "$key" in
    [!A-Za-z_]* | *[!A-Za-z0-9_]*) continue ;;
  esac
  val=${line#*=}
  printf '%s' "$val" > "$OUT_DIR/$key"
  chmod 600 "$OUT_DIR/$key"
  echo "wrote $OUT_DIR/$key"
  count=$((count + 1))
  [ -z "$val" ] && missing="$missing $key"
done < "$ENV_FILE"

echo "wrote $count file(s) to $OUT_DIR/"
if [ -n "$missing" ]; then
  echo "" >&2
  echo "warning: these keys were empty in $ENV_FILE (wrote empty files):$missing" >&2
  echo "fill them in $ENV_FILE and re-run if the container needs them." >&2
fi

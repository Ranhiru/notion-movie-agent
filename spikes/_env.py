"""Shared env loading for the Phase 0 spikes.

Every spike calls `load()` at import time. It loads the project-root `.env` via
python-dotenv (without overriding anything already exported in the shell — so running
a spike from a shell that already has the secrets works too). `require()` gives a clear,
actionable error when a needed var is missing, instead of an opaque auth failure deep
in an API client.

This is throwaway spike glue; Phase 1 replaces it with a typed settings object.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


def load() -> None:
    """Load the project-root .env (shell-exported vars win — override=False)."""
    load_dotenv(_ENV_PATH, override=False)


def require(*names: str) -> list[str]:
    """Return the values for `names`, exiting with a clear message if any are unset/empty."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(
            "\n  Missing required env var(s): " + ", ".join(missing) + "\n"
            f"  Set them in {_ENV_PATH} (copy from .env.example) or export them in your shell,\n"
            "  then re-run this spike.\n",
            file=sys.stderr,
        )
        sys.exit(2)
    return [os.environ[n] for n in names]


def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


# Load on import so spikes can just `import _env` then read os.environ / _env.require(...).
load()

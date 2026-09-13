"""`python -m backend` -> the CLI in `backend/cli.py`."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

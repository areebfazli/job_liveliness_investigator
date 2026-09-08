"""Standalone entrypoint: `uv run python -m rli.api` starts the API server.

Deliberately not wired into `rli/cli.py` (Phase 5 may not touch it) — this
is a separate, small `uvicorn.run` launcher. Host/port are environment
driven, matching `rli.api.settings.ApiSettings`'s conventions.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("RLI_API_HOST", "127.0.0.1")
    port = int(os.environ.get("RLI_API_PORT", "8000"))
    uvicorn.run("rli.api.app:app", host=host, port=port)


if __name__ == "__main__":
    main()

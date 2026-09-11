"""Standalone entrypoint: `uv run python -m rli.api` starts the API server.

Deliberately not wired into `rli/cli.py` (Phase 5 may not touch it) — this
is a separate, small `uvicorn.run` launcher. Everything it needs comes from
`rli.api.settings.ApiSettings` rather than from `os.environ` directly, so
the bind host this process actually uses is the same value the loopback/token
safety rule (`startup_security_error`) is evaluated against.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from rli.api.settings import ApiSettings, startup_security_error

_LOG = logging.getLogger("rli.api")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = ApiSettings.from_env()

    message = startup_security_error(settings)
    if message is not None:
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)

    if not settings.api_token:
        _LOG.warning(
            "RLI_API_TOKEN is not set: authentication is disabled and the API is "
            "relying on its loopback bind (%s) to keep /investigate, /watch and "
            "/outcomes private. Set RLI_API_TOKEN to require a bearer token.",
            settings.host,
        )

    uvicorn.run("rli.api.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

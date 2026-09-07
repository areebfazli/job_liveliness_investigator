"""ProbeResult — spec.md §2 structured probe failure shape."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ProbeResult(BaseModel):
    """Result of executing a probe.

    On success, `ok=True` and `data` carries the probe's structured facts.
    On failure, `ok=False`, `error` describes what went wrong, and
    `retryable` says whether a bounded retry is sensible (spec.md §2:
    "Structured probe failure: {ok:false,error,retryable}; no uncontrolled
    retry loops.").
    """

    ok: bool
    error: str | None = None
    retryable: bool = False
    data: dict[str, Any] | None = None

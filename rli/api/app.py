"""Phase 5 product shell: a thin FastAPI HTTP API over Systems A/B/C.

This module does not add anything to `rli.eval` or `rli.agent` — it only
calls `run_system_a` / `run_system_b` / `run_system_c` against a per-request
sqlite3 connection and reshapes their `RunResult` into JSON. It owns no
evaluation logic of its own.

Settings that would normally live in `rli.config.Config` (db path, whether
the debug trace route is exposed, host/port, the watch-list JSON file path,
the bearer token, the per-client rate limit) are instead environment-driven
(`rli.api.settings.ApiSettings`) because `Config` is frozen with
`extra="forbid"` and Phase 5 is not allowed to edit `rli/config.py` or
`config.toml`. See `rli/api/settings.py`'s docstring for the full rationale.

Authentication: every endpoint that spends LLM quota, fetches from the
network, or writes to the database requires `Authorization: Bearer
<RLI_API_TOKEN>` when that variable is set, and is open when it is not —
the unauthenticated mode is safe only behind the default loopback bind,
which `rli.api.settings.startup_security_error` enforces at process start.
`GET /health` and `GET /` (the UI shell, which holds no data of its own)
are always open so a health check and a browser can reach them.

Connection lifecycle: one `sqlite3.Connection` is opened per request (via
`rli.db.connect`) and always closed in a `finally` block. FastAPI runs sync
route handlers in a threadpool, and sqlite3 connections are not safe to
share across threads, so a single long-lived connection would be a bug
waiting to happen under concurrent requests.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from rli.api.ratelimit import RateLimiter
from rli.api.settings import ApiSettings
from rli.api.watch_logic import add_watch, check_due_watches
from rli.config import load_config
from rli.db import connect, init_db
from rli.eval.runner import RunResult
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.eval.system_r import run_system_r
from rli.llm.client import (
    CachedClient,
    OpenAICompatibleClient,
    close_llm_client,
    endpoint_unavailable_reason,
)
from rli.models.time import now_utc, parse_utc, to_utc_z
from rli.net import DisallowedHostError, check_allowed

_LOG = logging.getLogger("rli.api")

_UI_INDEX = Path(__file__).parent.parent / "ui" / "index.html"

SystemChoice = Literal["A", "B", "C", "R"]
OutcomeType = Literal["applied", "reply", "screen", "interview", "offer", "rejection", "silence"]


class InvestigateRequest(BaseModel):
    url: str
    system: SystemChoice | None = None


class OutcomeRequest(BaseModel):
    run_id: str | None = None
    posting_id: str | None = None
    outcome: OutcomeType
    occurred_at: str | None = None
    # Capped rather than unbounded: `notes` is persisted verbatim and echoed
    # back, so an unbounded field is a free write-amplification primitive.
    # Pydantic rejects an over-long note as a 422 before the handler runs.
    note: str | None = Field(default=None, max_length=2000)


class WatchRequest(BaseModel):
    url: str


def _run_model_calls_all_failed(conn: sqlite3.Connection, run_id: str) -> bool:
    """True iff `run_id` has at least one `model` step and every one errored.

    Same inspection `rli/agent/cli.py::_warn_if_model_calls_failed` performs:
    `run_system_c` catches `LLMError` internally and still produces a valid
    (but policy-only) `Decision`, so the only way to notice that every model
    call was swallowed is to look at the recorded trace. Zero model rows is
    NOT this condition — that's a legitimate hard-stop-before-first-call
    short circuit, not degradation.
    """
    rows = conn.execute(
        "SELECT error FROM run_steps WHERE run_id = ? AND component = 'model'",
        (run_id,),
    ).fetchall()
    if not rows:
        return False
    return all(row["error"] is not None for row in rows)


def _decision_response(
    result: RunResult,
    *,
    system_used: str,
    degraded: bool,
    degraded_reason: str | None,
) -> dict:
    payload = result.decision.model_dump(mode="json")
    payload.update(
        run_id=result.run_id,
        system_used=system_used,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
    return payload


def create_app(
    db_path: str | None = None,
    *,
    watch_store_path: str | None = None,
    debug_routes: bool | None = None,
    api_token: str | None = None,
    rate_limit_rpm: int | None = None,
) -> FastAPI:
    settings = ApiSettings.from_env(
        db_path=db_path,
        watch_store_path=watch_store_path,
        debug_routes=debug_routes,
        api_token=api_token,
        rate_limit_rpm=rate_limit_rpm,
    )
    limiter = RateLimiter(settings.rate_limit_rpm)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        """Reject the request unless it carries the configured bearer token.

        A no-op when no token is configured: that is the default single-user
        loopback mode, which the entrypoint refuses to start on a non-loopback
        bind. The comparison is `secrets.compare_digest` so a wrong token
        cannot be recovered a byte at a time from response timing.
        """
        if not settings.api_token:
            return
        prefix = "Bearer "
        provided = (
            authorization[len(prefix) :]
            if authorization and authorization.startswith(prefix)
            else ""
        )
        if not secrets.compare_digest(provided, settings.api_token):
            raise HTTPException(
                status_code=401,
                detail="unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def enforce_rate_limit(request: Request) -> None:
        """429 when this client has burned its per-minute budget.

        Applied only to the two routes that can spend money or reach the
        network on a single call (`/investigate`, `/watch/due`); the cheap
        local-read routes are covered by the token alone.
        """
        client_key = request.client.host if request.client else "unknown"
        if not limiter.allow(client_key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    auth_only = [Depends(require_auth)]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Deferred to app *startup* (not to `create_app` itself) so that
        # merely importing this module — e.g. `from rli.api.app import
        # create_app` in a test — never touches disk. Only an actual served
        # app (uvicorn, or a TestClient used as a context manager / making a
        # request) runs this, and by then `settings.db_path` already carries
        # whatever scratch path the caller injected.
        init_db(settings.db_path)
        if not settings.api_token:
            _LOG.warning(
                "RLI_API_TOKEN is not set: /investigate, /outcomes and /watch are "
                "open to anyone who can reach this process, which is safe only "
                "because it is trusting its bind host (%s) to be loopback.",
                settings.host,
            )
        yield

    app = FastAPI(title="Role-Liveness Investigator", lifespan=lifespan)
    app.state.settings = settings

    # ------------------------------------------------------------------ #
    # /investigate
    # ------------------------------------------------------------------ #
    @app.post("/investigate", dependencies=auth_only)
    def investigate(body: InvestigateRequest, request: Request) -> JSONResponse:
        enforce_rate_limit(request)

        cfg = load_config()
        try:
            check_allowed(body.url, cfg.allowlists.json_ld)
        except DisallowedHostError as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc

        requested_system = body.system or "C"
        degraded = False
        degraded_reason: str | None = None

        conn = connect(settings.db_path)
        try:
            if requested_system == "A":
                result = run_system_a(conn, cfg, body.url)
                system_used = "A"
            elif requested_system == "B":
                result = run_system_b(conn, cfg, body.url)
                system_used = "B"
            elif requested_system == "R":
                result = run_system_r(conn, cfg, body.url)
                system_used = "R"
            else:
                # Availability is decided per REQUEST, on the System C path
                # only, and never at startup: a bounded (1.5s) `GET
                # {base_url}/models` that runs no model and costs nothing.
                # Doing it here rather than in the lifespan hook means the
                # app still boots with the endpoint down, an operator who
                # starts `ollama serve` afterwards is picked up on the next
                # request with no restart, and nothing is cached that could
                # go stale in either direction. The cost is one cheap probe
                # per System C request, which is negligible next to the run
                # it gates.
                unavailable = endpoint_unavailable_reason(cfg)
                if unavailable is not None:
                    result = run_system_b(conn, cfg, body.url)
                    system_used = "B"
                    degraded = True
                    degraded_reason = f"{unavailable}; ran System B instead of System C"
                else:
                    from rli.agent.loop import run_system_c

                    # Built here rather than left to `run_system_c`'s own
                    # lazy factory so this request OWNS the client and can
                    # close it: one httpx connection pool per /investigate
                    # call would otherwise be released only by the garbage
                    # collector.
                    system_c_llm = CachedClient(OpenAICompatibleClient.from_config(cfg), conn)
                    try:
                        result = run_system_c(conn, cfg, body.url, system_c_llm)
                    finally:
                        close_llm_client(system_c_llm)
                    system_used = "C"
                    if _run_model_calls_all_failed(conn, result.run_id):
                        degraded = True
                        degraded_reason = "all System C model calls failed; decision is policy-only"

            payload = _decision_response(
                result,
                system_used=system_used,
                degraded=degraded,
                degraded_reason=degraded_reason,
            )
            return JSONResponse(payload)
        except HTTPException:
            raise
        except DisallowedHostError as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=502, detail="investigation failed") from None
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # /runs/{run_id} — internal/debug only
    # ------------------------------------------------------------------ #
    @app.get("/runs/{run_id}", dependencies=auth_only)
    def get_run(run_id: str, x_rli_debug: str | None = Header(default=None)) -> JSONResponse:
        if not settings.debug_routes or x_rli_debug != "1":
            raise HTTPException(status_code=404, detail="not found")

        conn = connect(settings.db_path)
        try:
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise HTTPException(status_code=404, detail="not found")
            step_rows = conn.execute(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            ).fetchall()
            return JSONResponse(
                {
                    "run": dict(run_row),
                    "steps": [dict(row) for row in step_rows],
                }
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # /outcomes
    # ------------------------------------------------------------------ #
    @app.post("/outcomes", dependencies=auth_only)
    def create_outcome(body: OutcomeRequest) -> JSONResponse:
        if not body.posting_id and not body.run_id:
            raise HTTPException(
                status_code=400, detail="exactly one of run_id or posting_id is required"
            )

        conn = connect(settings.db_path)
        try:
            posting_id = body.posting_id
            if not posting_id:
                run_row = conn.execute(
                    "SELECT posting_id FROM runs WHERE id = ?", (body.run_id,)
                ).fetchone()
                if run_row is None:
                    raise HTTPException(status_code=404, detail="run not found")
                posting_id = run_row["posting_id"]
                if not posting_id:
                    raise HTTPException(
                        status_code=400,
                        detail="this run never resolved a posting; cannot record an outcome for it",
                    )

            occurred_at = parse_utc(body.occurred_at) if body.occurred_at else now_utc()
            occurred_at_z = to_utc_z(occurred_at)

            # `outcomes` has no uniqueness constraint (Phase 5 may not edit
            # rli/db/schema.sql), and the UI's "Save outcome" button is a
            # trivial double-click away from writing the same event twice.
            # SELECT-then-INSERT, so two *concurrent* identical requests can
            # still both land: an accepted residual for a single-user local
            # tool, and strictly better than no check at all.
            duplicate = conn.execute(
                """
                SELECT 1 FROM outcomes
                WHERE posting_id = ? AND outcome_type = ? AND occurred_at = ?
                """,
                (posting_id, body.outcome, occurred_at_z),
            ).fetchone()
            if duplicate is not None:
                raise HTTPException(status_code=409, detail="duplicate outcome")

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO outcomes (posting_id, outcome_type, occurred_at, notes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (posting_id, body.outcome, occurred_at_z, body.note),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=400, detail="unknown posting_id") from exc

            return JSONResponse(
                status_code=201,
                content={
                    "id": cursor.lastrowid,
                    "posting_id": posting_id,
                    "outcome": body.outcome,
                    "occurred_at": occurred_at_z,
                    "note": body.note,
                },
            )
        except HTTPException:
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # /watch
    # ------------------------------------------------------------------ #
    @app.post("/watch", dependencies=auth_only)
    def post_watch(body: WatchRequest) -> JSONResponse:
        cfg = load_config()
        try:
            check_allowed(body.url, cfg.allowlists.json_ld)
        except DisallowedHostError as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc

        conn = connect(settings.db_path)
        try:
            entry = add_watch(conn, cfg, body.url, settings.watch_store_path)
            return JSONResponse(entry)
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=502, detail="watch failed") from None
        finally:
            conn.close()

    @app.get("/watch", dependencies=auth_only)
    def get_watch() -> JSONResponse:
        from rli.api.watch_store import load_watches

        return JSONResponse(load_watches(settings.watch_store_path))

    @app.get("/watch/due", dependencies=auth_only)
    def get_watch_due(request: Request) -> JSONResponse:
        enforce_rate_limit(request)

        cfg = load_config()
        conn = connect(settings.db_path)
        try:
            results = check_due_watches(conn, cfg, settings.watch_store_path)
            return JSONResponse(results)
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # /health
    # ------------------------------------------------------------------ #
    @app.get("/health")
    def health() -> JSONResponse:
        cfg_llm = load_config().llm
        # Deliberately does NOT probe the endpoint: /health is polled (by a
        # container orchestrator, by the demo script, by a human refreshing)
        # and must answer instantly, whereas the probe can spend up to
        # `DEFAULT_PROBE_TIMEOUT_S` waiting on a socket. This reports what is
        # CONFIGURED — a local base_url, or a key present in the environment
        # variable `[llm].api_key_env` names. `/investigate` does the live
        # probe, once, on the request that actually needs System C.
        return JSONResponse(
            {
                "status": "ok",
                "llm_configured": cfg_llm.credentials_configured(),
                "llm_base_url": cfg_llm.base_url,
                "llm_model_id": cfg_llm.model_id,
            }
        )

    # ------------------------------------------------------------------ #
    # / — the product UI
    # ------------------------------------------------------------------ #
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_UI_INDEX)

    return app


app = create_app()

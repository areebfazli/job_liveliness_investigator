"""Phase 5 product shell: a thin FastAPI HTTP API over Systems A/B/C.

This module does not add anything to `rli.eval` or `rli.agent` — it only
calls `run_system_a` / `run_system_b` / `run_system_c` against a per-request
sqlite3 connection and reshapes their `RunResult` into JSON. It owns no
evaluation logic of its own.

Settings that would normally live in `rli.config.Config` (db path, whether
the debug trace route is exposed, host/port, the watch-list JSON file path)
are instead environment-driven (`rli.api.settings.ApiSettings`) because
`Config` is frozen with `extra="forbid"` and Phase 5 is not allowed to edit
`rli/config.py` or `config.toml`. See `rli/api/settings.py`'s docstring for
the full rationale.

Connection lifecycle: one `sqlite3.Connection` is opened per request (via
`rli.db.connect`) and always closed in a `finally` block. FastAPI runs sync
route handlers in a threadpool, and sqlite3 connections are not safe to
share across threads, so a single long-lived connection would be a bug
waiting to happen under concurrent requests.
"""

from __future__ import annotations

import sqlite3
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from rli.api.settings import ApiSettings
from rli.api.watch_logic import add_watch, check_due_watches
from rli.config import load_config
from rli.db import connect, init_db
from rli.eval.runner import RunResult
from rli.eval.system_a import run_system_a
from rli.eval.system_b import run_system_b
from rli.llm.client import (
    CachedClient,
    OpenAICompatibleClient,
    close_llm_client,
    endpoint_unavailable_reason,
)
from rli.models.time import now_utc, parse_utc, to_utc_z
from rli.net import DisallowedHostError, check_allowed

_UI_INDEX = Path(__file__).parent.parent / "ui" / "index.html"

SystemChoice = Literal["A", "B", "C"]
OutcomeType = Literal["applied", "reply", "screen", "interview", "offer", "rejection", "silence"]


class InvestigateRequest(BaseModel):
    url: str
    system: SystemChoice | None = None


class OutcomeRequest(BaseModel):
    run_id: str | None = None
    posting_id: str | None = None
    outcome: OutcomeType
    occurred_at: str | None = None
    note: str | None = None


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
) -> FastAPI:
    settings = ApiSettings.from_env(db_path=db_path, watch_store_path=watch_store_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Deferred to app *startup* (not to `create_app` itself) so that
        # merely importing this module — e.g. `from rli.api.app import
        # create_app` in a test — never touches disk. Only an actual served
        # app (uvicorn, or a TestClient used as a context manager / making a
        # request) runs this, and by then `settings.db_path` already carries
        # whatever scratch path the caller injected.
        init_db(settings.db_path)
        yield

    app = FastAPI(title="Role-Liveness Investigator", lifespan=lifespan)
    app.state.settings = settings

    # ------------------------------------------------------------------ #
    # /investigate
    # ------------------------------------------------------------------ #
    @app.post("/investigate")
    def investigate(body: InvestigateRequest) -> JSONResponse:
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
    @app.get("/runs/{run_id}")
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
    @app.post("/outcomes")
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

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO outcomes (posting_id, outcome_type, occurred_at, notes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (posting_id, body.outcome, to_utc_z(occurred_at), body.note),
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
                    "occurred_at": to_utc_z(occurred_at),
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
    @app.post("/watch")
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

    @app.get("/watch")
    def get_watch() -> JSONResponse:
        from rli.api.watch_store import load_watches

        return JSONResponse(load_watches(settings.watch_store_path))

    @app.get("/watch/due")
    def get_watch_due() -> JSONResponse:
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

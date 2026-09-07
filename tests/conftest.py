"""Shared fixtures for adapter/probe tests (M1: resolvers + probes).

`ctx_factory` builds a `ProbeContext` whose `NetClient`s never really sleep
(injected `sleep=lambda *_: None`), matching the pattern `tests/test_net.py`
already uses so that a transport-failure test exercising the full retry
loop stays instant instead of taking real wall-clock backoff time.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from rli.config import Config, load_config
from rli.db import connect, init_db
from rli.models.time import now_utc
from rli.net import NetClient, RateLimiter, ToolCache
from rli.probes.base import ProbeContext


@pytest.fixture
def cfg() -> Config:
    return load_config()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "rli.sqlite3"
    init_db(db_path)
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def net_client_factory(
    cfg: Config, conn: sqlite3.Connection
) -> Iterator[Callable[[str], NetClient]]:
    """`ctx.net_client_factory`: one no-real-sleep `NetClient` per probe name."""
    created: list[NetClient] = []

    def factory(probe_name: str) -> NetClient:
        client = NetClient(
            client=httpx.Client(timeout=1.0),
            rate_limiter=RateLimiter(default_rps=1_000.0, default_burst=1_000),
            cache=ToolCache(conn),
            probe=probe_name,
            allowlist=getattr(cfg.allowlists, probe_name),
            max_retries=cfg.net.max_retries,
            backoff_base_s=cfg.net.backoff_base_s,
            max_backoff_s=cfg.net.max_backoff_s,
            max_redirects=cfg.net.max_redirects,
            sleep=lambda _seconds: None,
        )
        created.append(client)
        return client

    try:
        yield factory
    finally:
        for client in created:
            client.close()


@pytest.fixture
def ctx_factory(
    cfg: Config, conn: sqlite3.Connection, net_client_factory: Callable[[str], NetClient]
) -> Callable[..., ProbeContext]:
    def build(*, now: Callable[[], object] = now_utc) -> ProbeContext:
        return ProbeContext(
            conn=conn,
            config=cfg,
            net_client_factory=net_client_factory,
            now=now,
        )

    return build

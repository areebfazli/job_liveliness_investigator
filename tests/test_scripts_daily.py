"""Guards on `scripts/daily.sh` — the unattended weekly history rebuild.

This script is not imported by anything, so nothing else would notice it
breaking until a timer fired at 00:05 and the laptop fell over. The checks
here are deliberately shallow (syntax, plus the memory cap being present and
wrapped around the right command); the rebuild's own behaviour is tested in
`tests/test_history_rebuild_scale.py`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DAILY_SH = Path(__file__).resolve().parents[1] / "scripts" / "daily.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return DAILY_SH.read_text(encoding="utf-8")


def test_daily_sh_parses() -> None:
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to run scripts/daily.sh"

    result = subprocess.run(  # noqa: S603 - fixed argv, repo-local script
        [bash, "-n", str(DAILY_SH)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_the_weekly_rebuild_is_memory_capped(script: str) -> None:
    """The 2026-09-21 incident guard.

    A rebuild that grows unbounded must be killed by the cgroup rather than
    by the kernel OOM killer picking a victim across the whole machine. Both
    halves matter: `MemoryMax` bounds RSS, and `MemorySwapMax=0` stops a
    runaway from thrashing the desktop into swap first.
    """
    assert "MemoryMax=3G" in script
    assert "MemorySwapMax=0" in script

    cap = re.search(
        r"REBUILD_CMD=\(systemd-run --user --scope[^\n]*\"\$\{REBUILD_CMD\[@\]\}\"\)", script
    )
    assert cap is not None, "the cap must wrap the rebuild command, not sit beside it"


def test_the_cap_degrades_to_a_plain_invocation(script: str) -> None:
    """A missing or unusable `systemd-run` must not skip the rebuild."""
    assert "command -v systemd-run" in script
    assert "rebuild runs uncapped" in script


def test_the_rebuild_peak_is_logged(script: str) -> None:
    """Memory drift has to be visible in the log before it is an incident."""
    assert "/usr/bin/time" in script
    assert "Maximum resident set size" in script
    assert "history rebuild peak RSS" in script


def test_a_failed_rebuild_still_fails_the_script(script: str) -> None:
    """A cgroup kill (rc 137) must surface, not be swallowed by the logging."""
    assert 'exit "$REBUILD_RC"' in script
    assert "137" in script

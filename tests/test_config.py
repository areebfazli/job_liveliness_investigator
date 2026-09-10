"""Tests for the config.toml loader."""

from __future__ import annotations

import copy
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rli.config import REPO_ROOT, Config, load_config

MINIMAL_CONFIG_TOML = """
[thresholds]
recheck_default_days = 14
recheck_cap_days = 14
max_dynamic_steps = 4
recent_publish_days = 14
long_lived_days = 180

[budgets]
max_cost_usd = 0.50
max_latency_s = 60.0

[net]
max_retries = 3
backoff_base_s = 0.5
max_backoff_s = 30.0
timeout_s = 20.0
max_redirects = 5
default_requests_per_second = 1.0
default_burst = 3

[rate_limits."example.com"]
requests_per_second = 1.0
burst = 3

[allowlists]
resolve_posting = ["example.com"]
board_snapshot = []
repost_history = []
requirements_drift = []
company_events = []
team_signal = []
json_ld = ["*"]

[policy]
"""


def _real_config_dict() -> dict[str, Any]:
    with (REPO_ROOT / "config.toml").open("rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


def test_load_config_from_repo_root_with_no_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RLI_CONFIG", raising=False)
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.thresholds.recheck_default_days == 14


def test_load_config_honours_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "custom.toml"
    config_path.write_text(MINIMAL_CONFIG_TOML)
    monkeypatch.setenv("RLI_CONFIG", str(config_path))
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.allowlists.resolve_posting == ["example.com"]


def test_explicit_path_beats_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / "env.toml"
    env_path.write_text(MINIMAL_CONFIG_TOML)
    monkeypatch.setenv("RLI_CONFIG", str(env_path))

    explicit_path = tmp_path / "explicit.toml"
    explicit_content = MINIMAL_CONFIG_TOML.replace(
        'resolve_posting = ["example.com"]', 'resolve_posting = ["other.example"]'
    )
    explicit_path.write_text(explicit_content)

    cfg = load_config(explicit_path)
    assert cfg.allowlists.resolve_posting == ["other.example"]


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.toml")


# ---------------------------------------------------------------------------
# Real config content
# ---------------------------------------------------------------------------


def test_real_config_content() -> None:
    cfg = load_config(REPO_ROOT / "config.toml")

    assert cfg.thresholds.recheck_cap_days == 14
    assert cfg.allowlists.json_ld == ["*"]
    assert "boards-api.greenhouse.io" in cfg.allowlists.resolve_posting
    assert cfg.net.max_retries >= 0

    for name, rate_limit in cfg.rate_limits.items():
        assert rate_limit.requests_per_second > 0, name
        assert rate_limit.burst >= 1, name

    assert cfg.policy.frozen_at is None


# ---------------------------------------------------------------------------
# Invalid values raise ValidationError
# ---------------------------------------------------------------------------


def _mutated(mutate) -> dict[str, Any]:
    data = copy.deepcopy(_real_config_dict())
    mutate(data)
    return data


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda d: d["rate_limits"]["boards-api.greenhouse.io"].__setitem__(
                "requests_per_second", 0
            ),
            id="requests_per_second_zero",
        ),
        pytest.param(
            lambda d: d["rate_limits"]["boards-api.greenhouse.io"].__setitem__(
                "requests_per_second", -1
            ),
            id="requests_per_second_negative",
        ),
        pytest.param(
            lambda d: d["rate_limits"]["boards-api.greenhouse.io"].__setitem__("burst", 0),
            id="burst_zero",
        ),
        pytest.param(
            lambda d: d["net"].__setitem__("max_retries", -1),
            id="net_max_retries_negative",
        ),
        pytest.param(
            lambda d: d["net"].__setitem__("backoff_base_s", 0),
            id="net_backoff_base_zero",
        ),
        pytest.param(
            lambda d: (
                d["net"].__setitem__("backoff_base_s", 10.0),
                d["net"].__setitem__("max_backoff_s", 1.0),
            ),
            id="net_max_backoff_below_base",
        ),
        pytest.param(
            lambda d: d["matching"].__setitem__("title_min", 1.5),
            id="similarity_above_one",
        ),
    ],
)
def test_invalid_values_raise_validation_error(mutate) -> None:
    data = _mutated(mutate)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


# ---------------------------------------------------------------------------
# extra="forbid"
# ---------------------------------------------------------------------------


def test_unknown_key_in_allowlists_rejected() -> None:
    data = _mutated(lambda d: d["allowlists"].__setitem__("resolve_postings", ["typo.example"]))
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_unknown_key_at_root_rejected() -> None:
    data = _mutated(lambda d: d.__setitem__("bogus_top_level_key", 1))
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_unknown_key_in_net_rejected() -> None:
    data = _mutated(lambda d: d["net"].__setitem__("max_retires", 3))
    with pytest.raises(ValidationError):
        Config.model_validate(data)


# ---------------------------------------------------------------------------
# policy.frozen_at
# ---------------------------------------------------------------------------


def test_policy_frozen_at_naive_rejected() -> None:
    data = _mutated(
        lambda d: d.setdefault("policy", {}).__setitem__("frozen_at", datetime(2026, 1, 1))
    )
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_policy_frozen_at_non_utc_normalized() -> None:
    aware = datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    data = _mutated(lambda d: d.setdefault("policy", {}).__setitem__("frozen_at", aware))
    cfg = Config.model_validate(data)
    assert cfg.policy.frozen_at == aware.astimezone(UTC)
    assert cfg.policy.frozen_at.utcoffset() == timedelta(0)

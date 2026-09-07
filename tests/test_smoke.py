"""Smoke test: rli and all its subpackages import cleanly."""

import importlib

SUBPACKAGES = [
    "rli",
    "rli.config",
    "rli.db",
    "rli.models",
    "rli.models.time",
    "rli.net",
    "rli.resolvers",
    "rli.snapshots",
    "rli.archive",
    "rli.history",
    "rli.events",
    "rli.probes",
    "rli.policy",
    "rli.agent",
    "rli.llm",
    "rli.replay",
    "rli.eval",
    "rli.api",
    "rli.cli",
]


def test_all_subpackages_import() -> None:
    for name in SUBPACKAGES:
        importlib.import_module(name)

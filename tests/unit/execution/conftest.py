"""Shared fixtures for execution unit tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from application_sdk.contracts.base import Input, Output

# The shared gate-outcome capture, in scope for the whole directory the way a
# consumer adopts it (docs/agents/testing.md).
from application_sdk.testing import capture_preflight_outcomes  # noqa: F401

_GATE_LOGGER_NAME = "application_sdk.execution._temporal.preflight_gate"


@pytest.fixture
def gate_records(
    loguru_capture: list[dict[str, Any]],
) -> Callable[..., list[dict[str, Any]]]:
    """The preflight gate's log records as the real sink received them.

    For gate lines other than the outcome row (which
    ``capture_preflight_outcomes`` reads). Call with a loguru level name to
    filter, e.g. ``gate_records("WARNING")``; each item is a raw loguru record,
    bound fields under ``["extra"]``.
    """

    def _read(level: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in loguru_capture
            if r["extra"].get("logger_name") == _GATE_LOGGER_NAME
            and (level is None or r["level"].name == level)
        ]

    return _read


@dataclass
class SimpleExecInput(Input, allow_unbounded_fields=True):
    name: str = "World"


@dataclass
class SimpleExecOutput(Output, allow_unbounded_fields=True):
    greeting: str = ""

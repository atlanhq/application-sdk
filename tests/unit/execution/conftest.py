"""Shared fixtures for execution unit tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from application_sdk.contracts.base import Input, Output

# The shared gate-outcome capture, in scope for the whole directory the way a
# consumer adopts it (docs/agents/testing.md).
from application_sdk.testing import capture_preflight_outcomes  # noqa: F401

_GATE_LOGGER_NAME = "application_sdk.execution._temporal.preflight_gate"


@pytest.fixture(autouse=True)
def _no_leaked_app_state() -> Iterator[None]:
    """Drop every run's app-state slot a test leaves behind.

    Activities run here track their refs in ``_app_state``; the run-state
    sweeper visits every slot, so one leaked by an earlier test would show up
    as a run a later sweep test never told its fake Temporal about.
    """
    from application_sdk.app.base import _app_state, _app_state_lock

    yield
    with _app_state_lock:
        _app_state.clear()


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

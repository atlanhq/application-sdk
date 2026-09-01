"""Reading the preflight gate's outcome row from a test.

The gate emits exactly one ``Preflight gate outcome`` row per invocation and
levels it from the verdict (FND-901): ``error`` when the run is blocked or the
source is unverifiable, ``warning`` when it proceeded with a failed advisory
check, ``info`` otherwise. A test that reads the row therefore has to scan all
three levels — one that watches ``info`` alone sees an empty list on exactly the
runs it was written to pin, and passes silently until a verdict changes level.

That is not hypothetical. ``atlan-powerbi-app`` shipped an ``info``-only reader
twelve days before FND-901 moved blocks to ``error``; four block-path tests then
asserted against an empty list. The reader was correct when written and became
wrong without being touched, which is the failure mode a shared helper exists to
prevent.

Two entry points, because there are two ways a suite already patches the gate's
logger:

* :func:`capture_preflight_outcomes` — a pytest fixture that installs the
  recorder itself and yields it. Prefer this in connector suites; there is
  nothing to patch and no level to get wrong.
* :func:`outcome_rows` / :func:`outcome_level` / :func:`single_outcome` — the
  same scan over a ``MagicMock`` logger, for suites that patch
  ``preflight_gate.logger`` themselves and want to keep doing so.

Both agree on one rule worth stating explicitly: a gate call emits *at most* one
outcome row, so :attr:`PreflightOutcomeCapture.one` and :func:`single_outcome`
assert exactly one rather than returning the first match. Returning the first
match hides a double emission, which is how a re-entrant ``_no_verdict`` once
slipped past the SDK's own suite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from unittest.mock import MagicMock

#: Levels the outcome row can be emitted at, in ascending severity. Kept in this
#: order so :func:`outcome_level` reports the level actually used rather than the
#: first one that happens to hold a call.
OUTCOME_LEVELS: tuple[str, ...] = ("info", "warning", "error")

_GATE_LOGGER_PATH = "application_sdk.execution._temporal.preflight_gate.logger"


def _outcome_event_name() -> str:
    from application_sdk.observability.events import (  # noqa: PLC0415 — deferred: keeps the events module off this module's import path
        PREFLIGHT_OUTCOME_EVENT,
    )

    return PREFLIGHT_OUTCOME_EVENT


class PreflightOutcomeCapture:
    """A stand-in for the gate's logger that keeps the rows it was handed.

    Records at every level in :data:`OUTCOME_LEVELS` and remembers which one
    carried each row, so a suite can assert the severity as well as the payload.
    Any other logger method is a no-op, so a gate that logs progress or debug
    detail does not pollute the capture.
    """

    def __init__(self) -> None:
        self._rows: list[tuple[str, dict[str, Any]]] = []

    def _record(self, level: str, message: str, **kwargs: Any) -> None:
        if message == _outcome_event_name():
            self._rows.append((level, kwargs))

    def info(self, message: str, *_args: Any, **kwargs: Any) -> None:
        self._record("info", message, **kwargs)

    def warning(self, message: str, *_args: Any, **kwargs: Any) -> None:
        self._record("warning", message, **kwargs)

    def error(self, message: str, *_args: Any, **kwargs: Any) -> None:
        self._record("error", message, **kwargs)

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: None

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Every outcome row captured, oldest first."""
        return [payload for _level, payload in self._rows]

    @property
    def levels(self) -> list[str]:
        """The level each captured row was emitted at, positionally aligned with :attr:`rows`."""
        return [level for level, _payload in self._rows]

    @property
    def one(self) -> dict[str, Any]:
        """The single outcome row, asserting exactly one was emitted."""
        rows = self.rows
        assert (
            len(rows) == 1
        ), f"expected exactly 1 outcome row, got {len(rows)}: {rows}"
        return rows[0]

    @property
    def level(self) -> str | None:
        """The level the outcome row was emitted at, or ``None`` if no row was."""
        levels = self.levels
        return levels[-1] if levels else None

    @property
    def matrix(self) -> list[dict[str, Any]]:
        """The single row's check matrix, decoded from its JSON payload."""
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — deferred: the key lives with the adapter, not with this helper
            CHECK_MATRIX_KEY,
        )

        return json.loads(self.one[CHECK_MATRIX_KEY])


@pytest.fixture
def capture_preflight_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PreflightOutcomeCapture]:
    """Patch the gate's logger with a :class:`PreflightOutcomeCapture` and yield it.

    Use this instead of patching ``preflight_gate.logger`` by hand: the capture
    reads every level the outcome row can arrive at, so a verdict that changes
    level does not silently empty the suite's assertions.
    """
    capture = PreflightOutcomeCapture()
    monkeypatch.setattr(_GATE_LOGGER_PATH, capture)
    yield capture


def outcome_rows(mock_logger: MagicMock) -> list[dict[str, Any]]:
    """Every outcome row a ``MagicMock`` gate logger was called with, oldest level first."""
    name = _outcome_event_name()
    return [
        call.kwargs
        for level in OUTCOME_LEVELS
        for call in getattr(mock_logger, level).call_args_list
        if call.args and call.args[0] == name
    ]


def outcome_level(mock_logger: MagicMock) -> str | None:
    """The level the outcome row was emitted at, or ``None`` if it never was."""
    name = _outcome_event_name()
    for level in OUTCOME_LEVELS:
        for call in getattr(mock_logger, level).call_args_list:
            if call.args and call.args[0] == name:
                return level
    return None


def single_outcome(mock_logger: MagicMock) -> dict[str, Any]:
    """The single outcome row from a ``MagicMock`` gate logger, asserting exactly one."""
    rows = outcome_rows(mock_logger)
    assert len(rows) == 1, f"expected exactly 1 outcome row, got {len(rows)}: {rows}"
    return rows[0]


def first_outcome_or_none(mock_logger: MagicMock) -> dict[str, Any] | None:
    """The first outcome row, or ``None`` — for paths asserting no row was emitted."""
    rows = outcome_rows(mock_logger)
    return rows[0] if rows else None

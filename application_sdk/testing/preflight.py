"""Reading the preflight gate's outcome row from a test.

The gate *activity* emits exactly one ``Preflight gate outcome`` row per
invocation and levels it from the verdict (FND-901): ``error`` when the run is
blocked or the source is unverifiable, ``warning`` when it proceeded with a
failed advisory check, ``info`` otherwise. A test that reads the row therefore
has to scan all three levels — one that watches ``info`` alone sees an empty
list on exactly the runs it was written to pin, and passes silently until a
verdict changes level.

**Boundary — what this module does and does not see.** Everything here reads
the gate activity's logger
(``application_sdk.execution._temporal.preflight_gate.logger``). The workflow
wrapper in ``application_sdk.app.base`` emits outcome rows of its own —
``skipped`` (``pre_gate_replay``, ``input_not_credential_resolvable``) and
``no_verdict`` — through Temporal's ``workflow.logger``, which this capture
does not patch (it is temporalio's module-level logger; replacing it globally
would swallow every workflow's logging, and its stdlib fallback repacks
structured fields into ``extra=``). A test pinning those paths must assert on
``workflow.logger`` directly; one that reaches for this capture there gets an
empty list — flagged here so that absence reads as "wrong logger", not "no
outcome".

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

#: Levels the outcome row can be emitted at, in ascending severity. Both entry
#: points scan all of them, so a verdict that changes level cannot silently
#: empty a suite's assertions.
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
    ``debug`` is a declared no-op, so gate progress logging does not pollute the
    capture; the gate calls nothing else on its logger.
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

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        """Deliberately dropped — the outcome row never arrives at debug.

        Declared explicitly rather than via a ``__getattr__`` catch-all: a
        catch-all answers *every* attribute (typos, protocol probes) with a
        callable, so a misspelled assertion silently passes.
        """

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
    level does not silently empty the suite's assertions. It covers the rows the
    gate *activity* emits; the workflow-emitted ``skipped``/``no_verdict`` rows
    go through ``workflow.logger`` instead — see the module docstring.
    """
    capture = PreflightOutcomeCapture()
    monkeypatch.setattr(_GATE_LOGGER_PATH, capture)
    yield capture


def _outcome_calls(mock_logger: MagicMock) -> list[tuple[str, dict[str, Any]]]:
    """``(level, payload)`` per captured row, in emission order.

    Read from ``mock_calls`` rather than each level's ``call_args_list`` so the
    result is chronological — the same order :class:`PreflightOutcomeCapture`
    keeps — and the two entry points cannot disagree on a double emission.
    """
    name = _outcome_event_name()
    return [
        (str(call[0]), dict(call.kwargs))
        for call in mock_logger.mock_calls
        if call[0] in OUTCOME_LEVELS and call.args and call.args[0] == name
    ]


def outcome_rows(mock_logger: MagicMock) -> list[dict[str, Any]]:
    """Every outcome row a ``MagicMock`` gate logger was called with, oldest first."""
    return [payload for _level, payload in _outcome_calls(mock_logger)]


def outcome_level(mock_logger: MagicMock) -> str | None:
    """The level the latest outcome row was emitted at, or ``None`` if no row was.

    Matches :attr:`PreflightOutcomeCapture.level`: on the (anomalous) double
    emission both report the most recent row's level.
    """
    calls = _outcome_calls(mock_logger)
    return calls[-1][0] if calls else None


def single_outcome(mock_logger: MagicMock) -> dict[str, Any]:
    """The single outcome row from a ``MagicMock`` gate logger, asserting exactly one."""
    rows = outcome_rows(mock_logger)
    assert len(rows) == 1, f"expected exactly 1 outcome row, got {len(rows)}: {rows}"
    return rows[0]


def first_outcome_or_none(mock_logger: MagicMock) -> dict[str, Any] | None:
    """The first outcome row, or ``None`` — for paths asserting no row was emitted."""
    rows = outcome_rows(mock_logger)
    return rows[0] if rows else None

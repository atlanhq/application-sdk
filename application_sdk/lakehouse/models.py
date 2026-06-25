"""Data models for the lakehouse package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class EventResult:
    """Per-event verdict returned by an events handler.

    The handler returns one ``EventResult`` per input event; ``events_ack``
    serialises these into the Parquet ack file the automation engine reads
    to mark events as processed / retried / failed.
    """

    status: Literal["SUCCESS", "RETRY", "FAILED"]
    error_message: str | None = None


@dataclass(frozen=True)
class EventsReadResult:
    """Outcome of an :func:`events_read` call.

    ``events`` and ``results`` align 1:1 (``results[i]`` is the verdict for
    ``events[i]``) and cover **every** event that was fetched. ``complete``
    is ``True`` when all fetched events were dispatched to the handler and
    ``False`` when the run aborted early (handler raised or returned a
    mismatched result count) — in the aborted case the failed batch and any
    not-yet-attempted events are marked ``RETRY`` so the caller can ack them
    and let the automation engine re-trigger, but ``complete=False`` lets the
    caller tell an abort apart from a clean run that merely contained
    per-event ``RETRY`` verdicts.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    results: list[EventResult] = field(default_factory=list)
    complete: bool = True

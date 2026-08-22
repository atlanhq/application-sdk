"""How much data an activity was given — the driver variable for tier sizing.

Peak memory alone says a tier is wrong; it cannot say what to key the tier *on*.
That needs the input size, so a rule can be fitted and evaluated before the
activity runs.

Two sources, in order:

1. **Reported bytes** — what the activity actually read, via
   :func:`report_input_bytes`. The SDK's own file readers call it, so any app going
   through them is covered without writing code; an app fetching data its own way
   calls it directly.
2. **``FileReference`` fields on the Input** — a fallback disk walk, for inputs the
   interceptor materialised rather than a reader pulling them.

Reported wins: it is what the activity read, where a ref only says what it was
handed. ``None`` means unknown, never 0 — a zero would fit a rule to inputs nobody
sized.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)

# A stat per file, so a pathological ref would walk a whole tree at activity end.
# Bounded here rather than trusted: the number is telemetry, and a partial count
# labelled ``truncated`` is more useful than an activity slowed down measuring
# itself.
_MAX_FILES_WALKED = 10_000


@dataclass(frozen=True)
class InputSize:
    """Bytes an activity read, and where the number came from.

    ``basis`` segments the data: ``reported`` is what the activity read,
    ``file_reference`` is what it was handed. Mixing them silently would fit one
    rule to two different definitions of "input".
    """

    bytes: int
    file_count: int
    basis: str
    truncated: bool = False


class InputCollector:
    """Accumulates bytes read during one activity execution.

    Created by the sizing interceptor and mutated in place, following
    ``OutputInterceptor``'s collector pattern. Mutation rather than reassignment is
    deliberate: a ContextVar *set* inside the activity may not be visible to the
    interceptor across a thread or context boundary, whereas a shared object is.
    """

    __slots__ = ("bytes", "file_count")

    def __init__(self) -> None:
        self.bytes = 0
        self.file_count = 0

    def add(self, num_bytes: int, file_count: int = 1) -> None:
        self.bytes += num_bytes
        self.file_count += file_count

    def has_data(self) -> bool:
        return self.file_count > 0


_current_inputs: ContextVar[InputCollector | None] = ContextVar(
    "sizing_input_collector", default=None
)


def begin_collection() -> InputCollector:
    """Start collecting for one activity. Called by the sizing interceptor."""
    collector = InputCollector()
    _current_inputs.set(collector)
    return collector


def end_collection() -> None:
    """Stop collecting, so a finished activity cannot leak into the next one."""
    _current_inputs.set(None)


def report_input_bytes(num_bytes: int, file_count: int = 1) -> None:
    """Report bytes this activity read, for tier-sizing telemetry.

    A no-op unless sizing collection is enabled, so it is safe to call
    unconditionally from a read path. Never raises.

    The SDK's own file readers call this, so most apps need not. Call it directly
    when an app fetches data the SDK cannot see — a driver query, a vendor client,
    or object-store reads it issues itself.
    """
    try:
        collector = _current_inputs.get()
        if collector is None or num_bytes < 0:
            return
        collector.add(num_bytes, file_count)
    # conformance: ignore[E004] telemetry on a read path; must never affect the read
    except Exception:
        _logger.debug("input byte reporting failed", exc_info=True)


def report_local_paths(paths: list[str]) -> None:
    """Report the on-disk size of files this activity read. Never raises."""
    try:
        if _current_inputs.get() is None:
            return
        for path in paths:
            try:
                report_input_bytes(os.path.getsize(path), 1)
            # conformance: ignore[E014] a file removed between read and stat; skip it
            except OSError:
                continue
    # conformance: ignore[E004] telemetry on a read path; must never affect the read
    except Exception:
        _logger.debug("input path reporting failed", exc_info=True)


def describe_inputs(input_data: Any) -> InputSize | None:
    """Size an activity's input, or ``None`` if it cannot be determined.

    Never raises: this runs beside a real activity and must not affect it.
    """
    try:
        collector = _current_inputs.get()
        if collector is not None and collector.has_data():
            return InputSize(
                bytes=collector.bytes,
                file_count=collector.file_count,
                basis="reported",
            )
        return _size_from_file_refs(input_data)
    # conformance: ignore[E004] telemetry; an unsizeable input is "unknown", not a failure
    except Exception:
        _logger.debug("input sizing failed", exc_info=True)
        return None


def _size_from_file_refs(input_data: Any) -> InputSize | None:
    """Sum the on-disk size of every materialised ``FileReference``."""
    from application_sdk.storage.file_ref_sync import (  # noqa: PLC0415 — circular: storage imports contracts which import observability
        _find_file_refs,
    )

    refs = _find_file_refs(input_data)
    if not refs:
        return None

    total = 0
    files = 0
    truncated = False
    for ref in refs:
        if not ref.local_path:
            # Durable but not materialised (``auto_materialize=False``). Sizing it
            # would cost an object-store call per activity, so the app that opted
            # out reports its own bytes instead.
            continue
        size, count, hit_cap = _walk(ref.local_path)
        total += size
        files += count
        truncated = truncated or hit_cap

    if files == 0:
        return None
    return InputSize(
        bytes=total, file_count=files, basis="file_reference", truncated=truncated
    )


def _walk(path: str) -> tuple[int, int, bool]:
    """``(bytes, file_count, hit_cap)`` for a file or directory tree."""
    if os.path.isfile(path):
        return os.path.getsize(path), 1, False

    total = 0
    files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            if files >= _MAX_FILES_WALKED:
                return total, files, True
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            # conformance: ignore[E014] a file removed mid-walk is expected; skip it
            except OSError:
                continue
    return total, files, False

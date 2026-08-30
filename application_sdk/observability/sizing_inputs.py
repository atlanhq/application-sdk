"""Input size — the driver variable a tier rule is keyed on. Reported bytes win
over a ``FileReference`` walk; ``None`` means unknown, never 0.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from application_sdk.common._listing import prune_internal_dirs
from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)

# One stat per file, so bound the walk: a partial count beats an activity slowed
# down measuring itself.
_MAX_FILES_WALKED = 10_000


@dataclass(frozen=True)
class InputSize:
    """Bytes read, plus the ``basis`` they came from — mixing "what it read" with
    "what it was handed" would fit one rule to two definitions of input.
    """

    bytes: int
    file_count: int
    basis: str
    truncated: bool = False


class InputCollector:
    """Accumulates bytes read during one execution. Mutated in place, not
    reassigned: a ContextVar set inside the activity may not cross a boundary.
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
    """Report bytes read. A no-op unless collection is on, so it is safe to call
    unconditionally; the SDK's readers already do, so most apps need not.
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
    """Size an activity's input, or ``None``. Never raises — it runs beside a real
    activity and must not affect it.
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
            # Not materialised, so sizing it would cost an object-store call per
            # activity; the app that opted out reports its own bytes.
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
    for root, dirs, names in os.walk(path):
        # SDK staging dirs hold in-flight duplicates of the artifacts beside
        # them — counting both would double the sizing metric.
        prune_internal_dirs(dirs)
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

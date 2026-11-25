"""What only the app knows: why lineage was not built, or why it can't be trusted.

The asset list, the Processes and the lookup refs are read from the app's own
output after the lineage step (:mod:`.reader`). The recorder holds the rest,
keyed by whatever key the app has at the call site (an id, a qualifiedName):

- :meth:`CoverageRecorder.missing`: this path did not build lineage, and why.
  Ignored for the verdict if the output shows lineage after all (another path
  built it); kept on the row as a failed attempt.
- :meth:`CoverageRecorder.partial`: lineage was built but is incomplete or may be
  wrong. The asset is INVALID even though the output shows lineage.
- :meth:`CoverageRecorder.unusable`: the output carries a Process for the asset,
  but the app knows it cannot resolve. The asset is MISSING.
- :meth:`CoverageRecorder.note`: dimensions for the row (connection types, kind).
- :meth:`CoverageRecorder.field_missing`: a count per parent and reason; fields
  get no state of their own.

Memory grows with failures and noted assets, not with assets. Every method is
guarded: an error stops recording and is swallowed (:mod:`.guard`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from application_sdk.observability.lineage.guard import guarded
from application_sdk.observability.lineage.taxonomy import LineageReason

__all__ = [
    "CoverageRecorder",
    "NoOpCoverageRecorder",
    "Recorded",
    "create_recorder",
]

Reason = type[LineageReason] | str

_MAX_DETAILS = 8


def _code(reason: Reason) -> str:
    return reason if isinstance(reason, str) else reason.code


@dataclass
class Recorded:
    """Everything recorded for one key."""

    missing: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    unusable: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    field_missing: Counter[str] = field(default_factory=Counter)


class CoverageRecorder:
    """Single-threaded, in memory. One per lineage step."""

    def __init__(self) -> None:
        self.enabled = True
        self.error: str | None = None
        self._by_key: dict[str, Recorded] = {}

    def _get(self, key: str) -> Recorded:
        entry = self._by_key.get(key)
        if entry is None:
            entry = self._by_key[key] = Recorded()
        return entry

    def _add(
        self,
        codes: list[str],
        entry: Recorded,
        reason: Reason,
        details: Mapping[str, Any] | None,
    ) -> None:
        code = _code(reason)
        if code not in codes:
            codes.append(code)
        if details and len(entry.details) < _MAX_DETAILS:
            entry.details.setdefault(code, dict(details))

    @guarded
    def missing(
        self, key: str, reason: Reason, details: Mapping[str, Any] | None = None
    ) -> None:
        entry = self._get(str(key))
        self._add(entry.missing, entry, reason, details)

    @guarded
    def partial(
        self, key: str, reason: Reason, details: Mapping[str, Any] | None = None
    ) -> None:
        entry = self._get(str(key))
        self._add(entry.partial, entry, reason, details)

    @guarded
    def unusable(
        self, key: str, reason: Reason, details: Mapping[str, Any] | None = None
    ) -> None:
        entry = self._get(str(key))
        self._add(entry.unusable, entry, reason, details)

    @guarded
    def note(self, key: str, **attributes: Any) -> None:
        entry = self._get(str(key))
        entry.attributes.update({k: v for k, v in attributes.items() if v is not None})

    @guarded
    def field_missing(self, parent_key: str, reason: Reason, count: int = 1) -> None:
        self._get(str(parent_key)).field_missing[_code(reason)] += count

    def get(self, key: str) -> Recorded | None:
        """What was recorded for ``key``; ``None`` if nothing. Never raises."""
        try:
            return self._by_key.get(str(key))
        except Exception:  # noqa: BLE001
            return None

    def __len__(self) -> int:
        return len(self._by_key)


class NoOpCoverageRecorder:
    """Coverage off: every call returns at once and nothing is held."""

    enabled = False
    error: str | None = None

    def missing(self, *args: Any, **kwargs: Any) -> None:
        return None

    partial = unusable = note = field_missing = missing

    def get(self, key: str) -> None:
        return None

    def __len__(self) -> int:
        return 0


def create_recorder(enabled: bool) -> CoverageRecorder | NoOpCoverageRecorder:
    """A recorder, or the no-op when coverage is off. Never raises."""
    try:
        return CoverageRecorder() if enabled else NoOpCoverageRecorder()
    except Exception:  # noqa: BLE001
        return NoOpCoverageRecorder()

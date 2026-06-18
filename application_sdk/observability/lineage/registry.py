"""Reason-code taxonomy machinery (connector-agnostic).

The SDK owns the **top-level codes** — the stable :class:`ReasonCategory` enum
(in :mod:`application_sdk.observability.lineage.types`) — plus the generic
machinery here: the :class:`ReasonCode` subcode dataclass, the
:class:`ReasonCodeRegistry` container, and :func:`get_reason_category` lookup.

The SDK deliberately ships **no concrete reason-code registries**. Granular
**subcodes** are connector- or domain-specific behavior and live in their owning
repo (e.g. ``app/lineage/reasons.py`` in a connector, or the publish-app's ARS
registry), each subcode mapped to exactly one SDK category. This keeps the SDK
free of connector-specific variables, statuses, and behavior, and lets a
taxonomy change ship without an SDK release.

Governance: ``record_missing_reason`` accepts any string verbatim. The
emit/reduce layers call :meth:`ReasonCodeRegistry.category` (or
:func:`get_reason_category`) and WARN (never raise) on an unregistered code,
bucketing it into :attr:`ReasonCategory.UNINSTRUMENTED_PATH` for the metric
rollup. One subcode maps to exactly one category.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Mapping, Optional, Union

from application_sdk.observability.lineage.types import ReasonCategory, ReasonCode

LOGGER = logging.getLogger(__name__)


class ReasonCodeRegistry:
    """A validated mapping of connector subcode → :class:`ReasonCode`.

    Thin wrapper over a plain dict that adds category resolution and a
    drift-warning lookup. A connector constructs one from its own subcode dict
    (see e.g. a connector's ``app/lineage/reasons.py``); the SDK ships none.
    """

    def __init__(self, codes: Optional[Mapping[str, ReasonCode]] = None) -> None:
        self._codes: Dict[str, ReasonCode] = {}
        if codes:
            self.merge(codes)

    @classmethod
    def from_codes(cls, codes: Iterable[ReasonCode]) -> "ReasonCodeRegistry":
        return cls({rc.code: rc for rc in codes})

    def merge(self, codes: Mapping[str, ReasonCode]) -> "ReasonCodeRegistry":
        for key, rc in codes.items():
            if key != rc.code:
                raise ValueError(
                    f"Registry key {key!r} does not match ReasonCode.code {rc.code!r}"
                )
            if not isinstance(rc.category, ReasonCategory):
                raise ValueError(f"{key!r} maps to non-ReasonCategory {rc.category!r}")
            self._codes[key] = rc
        return self

    def get(self, code: str) -> Optional[ReasonCode]:
        return self._codes.get(code)

    def category(self, code: str) -> ReasonCategory:
        """Resolve *code* to its category, warning + defaulting on drift."""
        rc = self._codes.get(code)
        if rc is not None:
            return rc.category
        LOGGER.warning(
            "Unregistered lineage reason code %r; bucketing as UNINSTRUMENTED_PATH",
            code,
        )
        return ReasonCategory.UNINSTRUMENTED_PATH

    def __contains__(self, code: object) -> bool:
        return code in self._codes

    def __len__(self) -> int:
        return len(self._codes)

    def as_dict(self) -> Dict[str, ReasonCode]:
        return dict(self._codes)


def get_reason_category(
    reason_code: str,
    reason_registry: Union[Mapping[str, ReasonCode], ReasonCodeRegistry, None] = None,
) -> Optional[ReasonCategory]:
    """Look up the category for a reason code string.

    Returns ``None`` when no registry is supplied or the code is unknown. For the
    warn-and-default behavior, use :meth:`ReasonCodeRegistry.category` instead.
    Accepts either a plain ``{code: ReasonCode}`` mapping or a
    :class:`ReasonCodeRegistry`.
    """
    if reason_registry is None:
        return None
    if isinstance(reason_registry, ReasonCodeRegistry):
        rc = reason_registry.get(reason_code)
        return rc.category if rc else None
    rc = reason_registry.get(reason_code)
    return rc.category if rc else None


__all__ = [
    "ReasonCodeRegistry",
    "get_reason_category",
]

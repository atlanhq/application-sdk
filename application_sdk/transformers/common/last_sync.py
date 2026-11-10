"""Deprecated alias of the last-sync primitive (FND-2097, FND-4690).

The implementation moved to :mod:`application_sdk.common.last_sync`. It had
to: this package raises a ``DeprecationWarning`` on import and is removed in
v4.0, so the v3 asset-mapper seam could not import the primitive from here
without every v3 connector import emitting a deprecation it cannot act on.

Each name is served by a PEP 562 ``__getattr__`` — the same object, not a
copy, so ``isinstance``/identity checks hold across both import paths — with a
``DeprecationWarning`` naming the caller's line. The names are deliberately not
bound at module scope: a real binding resolves before ``__getattr__`` runs, and
the alias would never warn. ``_DEPRECATED_CONSTANTS`` is the shape
``docs/standards/symbols.md`` prescribes, and is what records these names in the
deprecated-symbol manifest so B001 flags the old import fleet-wide.

No ``__all__``, deliberately: declaring one is how a module opts into the
SDK capability manifest, and this is an alias for a public surface rather
than a second one.

.. deprecated:: 3.35.0
    Import from ``application_sdk.common.last_sync`` instead — removed in
    v4.0 with the rest of ``application_sdk.transformers``.
"""

from __future__ import annotations

import warnings

from application_sdk.common import last_sync as _last_sync

#: name -> (replacement, why). The shared sentence and the removal version live
#: in ``__getattr__`` below.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "LastSyncDetails": (
        "application_sdk.common.last_sync.LastSyncDetails",
        "the same class, re-homed out of the deprecated transformers package",
    ),
    "resolve_last_sync_details": (
        "application_sdk.common.last_sync.resolve_last_sync_details",
        "the same function, re-homed out of the deprecated transformers package",
    ),
    "set_last_sync_details_on_asset": (
        "application_sdk.common.last_sync.set_last_sync_details_on_asset",
        "the same function, re-homed out of the deprecated transformers package",
    ),
    "set_last_sync_details_on_assets_bulk": (
        "application_sdk.common.last_sync.set_last_sync_details_on_assets_bulk",
        "the same function, re-homed out of the deprecated transformers package",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the re-homed names once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"application_sdk.transformers.common.last_sync.{name} is deprecated; "
        f"use {replacement} instead — {note}. Will be removed in v4.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(_last_sync, name)

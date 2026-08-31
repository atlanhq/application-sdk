"""Incremental extraction — the connection-scoped persistent-state seam.

This package owns one question: **where does a connection's cross-run state
live, and how is its watermark read and written?** The answer is a single
object-store layout::

    persistent-artifacts/apps/{application_name}/connection/{connection_id}/

``get_persistent_s3_prefix`` produces that prefix and
``get_persistent_artifacts_path`` its local counterpart;
``fetch_marker_from_storage`` / ``persist_marker_to_storage`` read and write the
incremental marker beneath it.

Import these from here rather than re-deriving them. An app that assembles the
prefix itself, or parses ``connection_qualified_name`` itself, has forked a
decision this package owns — and the fork is invisible until a connection
qualified name separates the two implementations. That is CONNECT-1136: a miner
took the first numeric segment where this package takes the last, and *raised*
where this package warns and proceeds, so a tenant whose connections are named
rather than epoch-stamped crawled normally and never mined. The conformance
suite enforces this seam as ``P048``/``P049``.

The names in ``__all__`` are the supported surface; everything else in this
package is an implementation detail that may move.

Re-exports are resolved lazily through ``__getattr__``. ``helpers`` and
``marker`` both pull in ``application_sdk.storage``, and importing them eagerly
here would put the whole storage stack behind
``from application_sdk.common.incremental.incremental_errors import ...`` — a
module that deliberately depends on nothing but the error leaves, and which
``helpers`` itself imports function-locally to keep that boundary thin. The
capability-manifest extractor reads ``__all__`` statically, so the seam stays
documented either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from application_sdk.common.incremental.helpers import (
        extract_epoch_id_from_qualified_name,
        get_persistent_artifacts_path,
        get_persistent_s3_prefix,
    )
    from application_sdk.common.incremental.marker import (
        create_next_marker,
        fetch_marker_from_storage,
        persist_marker_to_storage,
        process_marker_timestamp,
    )

__all__ = [
    "create_next_marker",
    "extract_epoch_id_from_qualified_name",
    "fetch_marker_from_storage",
    "get_persistent_artifacts_path",
    "get_persistent_s3_prefix",
    "persist_marker_to_storage",
    "process_marker_timestamp",
]

#: Public name -> defining submodule, for the lazy ``__getattr__`` below.
_EXPORTS: dict[str, str] = {
    "create_next_marker": "marker",
    "extract_epoch_id_from_qualified_name": "helpers",
    "fetch_marker_from_storage": "marker",
    "get_persistent_artifacts_path": "helpers",
    "get_persistent_s3_prefix": "helpers",
    "persist_marker_to_storage": "marker",
    "process_marker_timestamp": "marker",
}


def __getattr__(name: str) -> Any:
    """Resolve a public re-export on first access (PEP 562)."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module  # noqa: PLC0415

    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value  # cache so later lookups skip this path
    return value


def __dir__() -> list[str]:
    """Include the lazy re-exports in ``dir()`` and tab completion."""
    return sorted({*globals(), *__all__})

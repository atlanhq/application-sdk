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

The names below are the supported surface; everything else in this package is an
implementation detail that may move.
"""

from __future__ import annotations

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

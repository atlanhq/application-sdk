"""Back-compat re-export of the last-sync primitive (FND-2097).

The implementation moved to :mod:`application_sdk.common.last_sync`. It had
to: this package raises a ``DeprecationWarning`` on import and is removed in
v4.0, so the v3 asset-mapper seam could not import the primitive from here
without every v3 connector import emitting a deprecation it cannot act on.

This module is a pure alias — same objects, not copies, so
``isinstance``/identity checks hold across both import paths. Existing
callers keep working unchanged until this package is removed.

No ``__all__``, deliberately: declaring one is how a module opts into the
SDK capability manifest, and this is an alias for a public surface rather
than a second one.

.. deprecated:: 3.35.0
    Import from ``application_sdk.common.last_sync`` instead — removed in
    v4.0 with the rest of ``application_sdk.transformers``.
"""

from __future__ import annotations

from application_sdk.common.last_sync import (  # noqa: F401
    LastSyncDetails,
    resolve_last_sync_details,
    set_last_sync_details_on_asset,
    set_last_sync_details_on_assets_bulk,
)

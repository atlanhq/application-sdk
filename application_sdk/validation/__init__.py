"""Offline, reusable data validation for Atlan asset writes.

This package is the SDK's *principle-based* validation scaffold (BLDX-1555). It
builds on the per-asset ``.validate()`` backbone that ``pyatlan_v9`` exposes on
every asset class — a purely local, dry-run check (required fields, qualified-name
format, and create-time hierarchy fields) that needs **no network call**. Pushing
validation this deep in the stack means the same check applies whether an asset is
produced via the app SDK, low-level ``pyatlan``, or MCP.

Two things live here:

* :func:`validate_asset` — run pyatlan_v9's ``.validate()`` on a single asset and
  return the error messages (never raises).
* :func:`validate_transformed_dir` — cycle through every transformed-output NDJSON
  record, deserialize it back into its concrete ``pyatlan_v9`` asset (kept as a
  ``msgspec.Struct`` throughout — no intermediate ``dict``), run per-asset
  validation, and additionally run an SDK-side **referential-integrity** second
  pass that flags orphan children whose parent ``(typeName, qualifiedName)`` is
  absent from the same batch.

The referential-integrity pass is intentionally an SDK concern, not a pyatlan one:
it is a cross-record check that a single asset's ``.validate()`` cannot make.
"""

from application_sdk.validation.assets import (
    AssetValidationFailure,
    AssetValidationReport,
    ReferentialFailure,
    validate_asset,
    validate_transformed_dir,
)

__all__ = [
    "AssetValidationFailure",
    "AssetValidationReport",
    "ReferentialFailure",
    "validate_asset",
    "validate_transformed_dir",
]

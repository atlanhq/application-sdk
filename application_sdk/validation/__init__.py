"""Offline, reusable data validation for Atlan artifacts and asset writes.

Two layers live here.

**Artifact validation** (ADR-0020) is the generic one: a thin, format-agnostic
wrapper that takes an app-owned declaration, dispatches on format, and owns only
the shared outcome — one report shape, one outcome event, one bounded drill-down
payload. Byte integrity (``storage/integrity.py``) attests that the bytes read are
the bytes written and is explicit that this proves nothing about the artifact being
semantically complete; this is that missing third leg. Its two orthogonal plug-in
seams — *where the declaration comes from* and *how a format is checked* — are
:mod:`application_sdk.validation.protocols`; the shared outcome surface is
:mod:`application_sdk.validation.artifacts`. The two schema sources that ship are
:mod:`application_sdk.validation.sources` (``ContractSource``, ``ModelSource`` —
there is no inline source), the format validator that ships is
:mod:`application_sdk.validation.ndjson` (``NdjsonValidator`` — a streaming,
constant-memory, per-record check, plus ``iter_ndjson_lines``, the one NDJSON walk
in the tree), and the public entry point is ``validate_artifact`` in
:mod:`application_sdk.validation.wrapper`.

**Asset validation** (BLDX-1555) is the concrete NDJSON x typed-model check that
predates the wrapper, built on the per-asset ``.validate()`` backbone that
``pyatlan_v9`` exposes on every asset class — a purely local, dry-run check
(required fields, qualified-name format, and create-time hierarchy fields) that
needs **no network call**. Pushing validation this deep in the stack means the same
check applies whether an asset is produced via the app SDK, low-level ``pyatlan``,
or MCP. It offers:

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

ADR-0020 folds asset validation into the wrapper as its NDJSON x ``ModelSource``
cell, and FND-690 did it: ``NdjsonValidator`` dispatches a model declaration to
:func:`validate_assets_as_artifact`, which is :func:`validate_transformed_dir`
reported in the shared shape. The scan, its process isolation and its outcome event
are unchanged — the event's name and every attribute key are preserved verbatim,
because dashboards and alert rules key off those exact strings and v3 has shipped
consumers. A refactor behind a stable event surface, not a rename.

**Standing dependency floor for this whole package**: no ``pyarrow``, ``pandas`` or
``pandera`` import anywhere in it. A JSON-only caller must never pay for a parquet
reader, and pandera stays test-only.
"""

from application_sdk.validation.artifacts import (
    ARTIFACT_FORMATS,
    FORMAT_NDJSON,
    FORMAT_PARQUET,
    ArtifactDeclaration,
    ArtifactFailureKind,
    ArtifactFieldType,
    ArtifactFieldTypeExtended,
    ArtifactFormat,
    ArtifactValidationFailure,
    ArtifactValidationOutcome,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
    artifact_validation_event_fields,
    artifact_validation_matrix_json,
)
from application_sdk.validation.assets import (
    AssetArtifactReport,
    AssetValidationFailure,
    AssetValidationReport,
    ReferentialFailure,
    asset_validation_event_fields,
    asset_validation_matrix_json,
    supports_asset_model,
    validate_asset,
    validate_assets_as_artifact,
    validate_transformed_dir,
)
from application_sdk.validation.ndjson import NdjsonValidator, iter_ndjson_lines
from application_sdk.validation.protocols import FormatValidator, SchemaSource
from application_sdk.validation.sources import (
    ArtifactDeclarationError,
    ContractSource,
    ModelSource,
    artifact_schema_paths,
    declared_artifact_fields,
)
from application_sdk.validation.wrapper import (
    builtin_format_validators,
    validate_artifact,
)

__all__ = [
    # Artifact validation (ADR-0020)
    "ARTIFACT_FORMATS",
    "FORMAT_NDJSON",
    "FORMAT_PARQUET",
    "ArtifactDeclaration",
    "ArtifactDeclarationError",
    "ArtifactFailureKind",
    "ArtifactFieldType",
    "ArtifactFieldTypeExtended",
    "ArtifactFormat",
    "ArtifactValidationFailure",
    "ArtifactValidationOutcome",
    "ArtifactValidationReport",
    "ContractSource",
    "DeclaredField",
    "FieldMapDeclaration",
    "FormatValidator",
    "ModelDeclaration",
    "ModelSource",
    "NdjsonValidator",
    "SchemaSource",
    "artifact_schema_paths",
    "artifact_validation_event_fields",
    "artifact_validation_matrix_json",
    "builtin_format_validators",
    "declared_artifact_fields",
    "iter_ndjson_lines",
    "validate_artifact",
    # Asset validation (BLDX-1555) — the NDJSON x ModelSource cell (FND-690)
    "AssetArtifactReport",
    "AssetValidationFailure",
    "AssetValidationReport",
    "ReferentialFailure",
    "asset_validation_event_fields",
    "asset_validation_matrix_json",
    "supports_asset_model",
    "validate_asset",
    "validate_assets_as_artifact",
    "validate_transformed_dir",
]

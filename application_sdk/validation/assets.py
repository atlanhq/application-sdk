"""Asset-write validation built on the pyatlan_v9 ``.validate()`` backbone.

See :mod:`application_sdk.validation` for the why. This module holds the typed
result models, the single-asset check, and the transformed-output directory
walk (per-asset validation + referential-integrity second pass).

Since FND-690 it is also **the artifact wrapper's NDJSON x ``ModelSource`` cell** —
same walk, reached through :func:`~application_sdk.validation.wrapper.validate_artifact`
and reported in the shared :class:`~application_sdk.validation.artifacts.ArtifactValidationReport`
shape by :func:`validate_assets_as_artifact`. Nothing above that fold-in line changed:
the fold-in is a projection over the findings this walk already produced, plus the
shipped ``ASSET_VALIDATION_EVENT`` attribute map moved here from ``app.base`` so the
event and the check that feeds it live together.

Design constraints worth preserving if you edit this file:

* **Decode through ``from_atlas_json``, not ``<ConcreteType>.from_json``.**
  The two disagree about where relationships live. ``from_json`` reads the
  strict nested shape and picks them up **only** from ``relationshipAttributes``;
  ``from_atlas_json`` flattens ``attributes`` and ``relationshipAttributes``
  together first, so it accepts both. Connector transformed output puts
  relationships in ``attributes`` on purpose (see ``serialize_entity`` in the
  connector apps — "publish app expects them there"), which the strict decoder
  silently dropped. It also resolves the concrete type itself and is ~6x
  faster. See :func:`_deserialize`.
* **Concrete type resolution is load-bearing.** A generic ``Asset`` has only the
  3-field base ``.validate()`` and drops every typed relationship attribute.
  Resolving the concrete class is what unlocks the ``for_creation`` hierarchy
  checks and the typed parent relationships the referential pass reads.
* **Relationships are discovered, not hard-coded.** The referential pass reads
  whatever relationship references each asset actually carries in the NDJSON
  (enumerated generically off the Struct's relationship-typed fields) and
  cross-validates that every referenced ``(typeName, qualifiedName)`` also
  appears as an emitted asset. There is deliberately no per-type parent map —
  Atlan has hundreds of relationships and the set changes constantly.
* **One NDJSON walk in the tree.** The line iterator this module used to own now
  lives in :mod:`application_sdk.validation.ndjson` as ``iter_ndjson_lines`` and is
  imported from there. It was already format-generic, and a second copy would be a
  second set of decisions about blank lines, file ordering and directory recursion
  to keep in sync.
* **Bounded memory.** The referential pass keys on the compound
  ``(typeName, qualifiedName)`` and spills both the present-asset set and the
  referenced-target set to disk via
  :class:`~application_sdk.common.spillable_dict.SpillableDict`, so a
  multi-million-asset batch never blows the heap.
"""

from __future__ import annotations

import functools
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterator

import msgspec
import orjson
from pyatlan_v9.model.assets import Asset
from pyatlan_v9.model.assets.referenceable import RelatedReferenceable
from pyatlan_v9.model.transform import from_atlas_json

from application_sdk.common.spillable_dict import SpillableDict
from application_sdk.constants import ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
from application_sdk.observability.logger_adaptor import (
    ASSET_VALIDATION_MATRIX_KEY,
    get_logger,
)
from application_sdk.validation.artifacts import (
    OUTCOME_ABSENT,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    ModelDeclaration,
)
from application_sdk.validation.ndjson import iter_ndjson_lines

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Typed result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetValidationFailure:
    """A single asset that failed per-asset (``.validate()``) checks."""

    file: str
    """NDJSON file the record came from ("" if validated in-memory)."""
    line: int
    """1-based line number within ``file`` (0 if validated in-memory)."""
    type_name: str
    """Atlas ``typeName`` of the offending asset (best-effort)."""
    qualified_name: str
    """``qualifiedName`` of the offending asset (best-effort)."""
    errors: list[str]
    """Messages surfaced by pyatlan_v9's ``.validate()`` (or a deserialize error)."""
    deserialize_error: bool = False
    """True when the record could not be decoded at all (counted in
    ``undeserializable``), as opposed to a decoded asset that failed ``.validate()``."""


@dataclass(frozen=True)
class ReferentialFailure:
    """A relationship reference whose target asset is absent from the batch.

    Captures the missing target plus a representative referencing asset. A single
    missing target (e.g. one un-emitted parent Table) is reported once, with
    ``reference_count`` recording how many assets in the batch pointed at it.
    """

    missing_type_name: str
    """``typeName`` of the referenced asset that is not present in the batch."""
    missing_qualified_name: str
    """``qualifiedName`` of the absent referenced asset."""
    reference_count: int
    """How many relationship references in the batch pointed at this target."""
    file: str
    """NDJSON file of a representative referencing asset."""
    line: int
    """1-based line of the representative referencing asset."""
    type_name: str
    """``typeName`` of the representative referencing asset."""
    qualified_name: str
    """``qualifiedName`` of the representative referencing asset."""
    relationship: str
    """The relationship attribute the reference came through (e.g. ``table``)."""


@dataclass
class AssetValidationReport:
    """Aggregate outcome of validating a batch of transformed assets."""

    total: int = 0
    """Total NDJSON records seen."""
    passed: int = 0
    """Records that passed per-asset validation (independent of orphan status)."""
    undeserializable: int = 0
    """Records that could not be decoded into a pyatlan_v9 asset."""
    failures: list[AssetValidationFailure] = field(default_factory=list)
    """Per-asset validation failures (includes deserialize failures)."""
    orphans: list[ReferentialFailure] = field(default_factory=list)
    """Referential-integrity failures from the second pass."""

    @property
    def ok(self) -> bool:
        """True when nothing failed on any axis."""
        return not self.failures and not self.orphans and self.undeserializable == 0

    @property
    def failed(self) -> int:
        """Count of per-asset validation failures."""
        return len(self.failures)

    def format_report(
        self, *, max_items: int = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
    ) -> str:
        """Render a human-readable summary.

        ``max_items`` caps how many failures/orphans are *listed* (per axis),
        never how many are examined — the headline counts always reflect the full
        batch. Defaults to :data:`~application_sdk.constants.ASSET_VALIDATION_MAX_ITEMS_PER_AXIS`,
        the same cap the upload activity applies to the structured
        ``asset_validation_matrix`` telemetry, so the two surfaces stay in lockstep.
        """
        # ``failed`` (len(failures)) includes the undeserializable records, so
        # report the two disjointly: "invalid" is the per-asset validation
        # failures only, with undeserializable counted separately.
        invalid = self.failed - self.undeserializable
        lines = [
            f"Asset validation: {self.passed}/{self.total} passed, "
            f"{invalid} invalid, {len(self.orphans)} orphaned, "
            f"{self.undeserializable} undeserializable"
        ]
        for failure in self.failures[:max_items]:
            loc = _location(failure.file, failure.line)
            if failure.deserialize_error:
                label = failure.qualified_name or "<unparseable record>"
                lines.append(
                    f"  UNDESERIALIZABLE {label}{loc}: " + "; ".join(failure.errors)
                )
            else:
                label = failure.qualified_name or "<no qualifiedName>"
                lines.append(
                    f"  INVALID [{failure.type_name or '?'}] {label}{loc}: "
                    + "; ".join(failure.errors)
                )
        # Split the overflow so undeserializable records are not miscounted as
        # "invalid assets" — matching the disjoint headline.
        remaining = self.failures[max_items:]
        extra_invalid = sum(1 for f in remaining if not f.deserialize_error)
        extra_undeser = sum(1 for f in remaining if f.deserialize_error)
        if extra_invalid > 0:
            lines.append(f"  ... and {extra_invalid} more invalid assets")
        if extra_undeser > 0:
            lines.append(f"  ... and {extra_undeser} more undeserializable records")
        for orphan in self.orphans[:max_items]:
            loc = _location(orphan.file, orphan.line)
            lines.append(
                f"  ORPHAN [{orphan.missing_type_name}] "
                f"{orphan.missing_qualified_name} referenced but not present in "
                f"batch — referenced by {orphan.reference_count} asset(s), e.g. "
                f"[{orphan.type_name}] {orphan.qualified_name}{loc} "
                f"via '{orphan.relationship}'"
            )
        extra_orphans = len(self.orphans) - max_items
        if extra_orphans > 0:
            lines.append(f"  ... and {extra_orphans} more orphans")
        return "\n".join(lines)


def _location(file: str, line: int) -> str:
    if file and line:
        return f" ({file}:{line})"
    if file:
        return f" ({file})"
    return ""


# ---------------------------------------------------------------------------
# Compound-key encoding + generic relationship discovery
# ---------------------------------------------------------------------------

_KEY_SEP = "\x00"
"""Separator for the ``(typeName, qualifiedName)`` compound key. NUL never
appears in a typeName or qualifiedName, so the encoding is unambiguous."""


def _compound_key(type_name: str, qualified_name: str) -> str:
    return f"{type_name}{_KEY_SEP}{qualified_name}"


def _usable_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty ``str``, else ``None``.

    pyatlan_v9 marks unset fields with an ``UNSET`` sentinel (not ``None``), so a
    concrete ``str`` check is the sentinel-agnostic way to ask "is this set?".
    """
    return value if isinstance(value, str) and value else None


def _annotation_is_relationship(annotation: object) -> bool:
    """True when a field annotation references a ``RelatedReferenceable`` subclass.

    Handles the ``Union[RelatedX, None, UnsetType]`` / ``Union[List[RelatedX],
    ...]`` shapes pyatlan_v9 uses for relationship fields by recursing into type
    arguments.
    """
    if isinstance(annotation, type):
        try:
            return issubclass(annotation, RelatedReferenceable)
        except TypeError:
            return False
    return any(_annotation_is_relationship(arg) for arg in typing.get_args(annotation))


@functools.lru_cache(maxsize=None)
def _relationship_field_names(cls: type) -> tuple[str, ...]:
    """Relationship-typed field names for a concrete asset class (cached per class).

    Derived from the class's own field annotations, so it tracks the real
    relationship set — no hand-maintained list to drift.
    """
    return tuple(
        f.name
        for f in msgspec.structs.fields(cls)
        if _annotation_is_relationship(f.type)
    )


def _iter_relationship_refs(asset: Asset) -> Iterator[tuple[str, str, str]]:
    """Yield ``(relationship_field, target_typeName, target_qualifiedName)``.

    Enumerates every relationship reference the asset actually carries — single
    or list-valued — that identifies its target by qualifiedName. References that
    only carry a guid (no qualifiedName) are skipped: they cannot be cross-checked
    against the qualifiedName-keyed present-set.
    """
    for name in _relationship_field_names(type(asset)):
        value = getattr(asset, name, None)
        if isinstance(value, RelatedReferenceable):
            candidates: tuple = (value,)
        elif isinstance(value, list):
            candidates = tuple(value)
        else:
            continue
        for ref in candidates:
            if not isinstance(ref, RelatedReferenceable):
                continue
            target_tn = _usable_str(getattr(ref, "type_name", None))
            target_qn = _usable_str(getattr(ref, "qualified_name", None))
            if target_tn and target_qn:
                yield name, target_tn, target_qn


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------


def _deserialize(raw: bytes) -> Asset:
    """Decode one NDJSON line into its concrete pyatlan_v9 asset Struct.

    Uses ``from_atlas_json`` rather than ``<ConcreteType>.from_json``. The two
    differ in a way that matters here: ``from_json`` decodes the strict nested
    shape and picks relationships up **only** from ``relationshipAttributes``,
    whereas ``from_atlas_json`` flattens ``attributes`` and
    ``relationshipAttributes`` together before converting.

    Connector transformed output puts relationships in ``attributes``. That is
    deliberate — see ``serialize_entity`` in the connector apps, "publish app
    expects them there" — so the strict decoder silently dropped every
    relationship, leaving ``column.table`` and friends UNSET and failing every
    create-time parent check. Fleet-wide that read as ~98% of assets invalid
    when nothing was actually wrong with them.

    The transformed-output schema is a cross-repo contract this repo consumes
    but does not own. Follow-up: FND-119 tracks documenting it explicitly (and
    adding a contract-level fixture) so a producer-side format change fails
    loudly here instead of drifting silently.

    ``from_atlas_json`` accepts both shapes, so this is a widening, not a swap:
    payloads that already carry relationships under ``relationshipAttributes``
    decode exactly as before. It also resolves the concrete type itself, which
    is why the typeName probe this used to need is gone, and it measures ~6x
    faster than the per-class nested decoder (≈200k vs ≈31k records/sec).

    Raises whatever msgspec/pyatlan raise on malformed input — callers convert
    that into an ``undeserializable`` count rather than letting it abort a batch.
    """
    return from_atlas_json(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_asset(asset: Asset, *, for_creation: bool = True) -> list[str]:
    """Run pyatlan_v9's ``.validate()`` and return its error messages.

    Returns an empty list when the asset is valid. Never raises — a failed
    validation surfaces as the returned messages.

    Args:
        asset: A concrete pyatlan_v9 asset instance.
        for_creation: When True (default), also enforce the create-time hierarchy
            checks. Connector runs currently assume a first-time run against a
            source, so everything they emit as transformed output is for initial
            creation.
    """
    try:
        asset.validate(for_creation=for_creation)
    except Exception as exc:  # noqa: BLE001 — "Never raises": any validate() error surfaces as a message, never aborts the batch
        return [str(exc)]
    return []


def validate_transformed_dir(
    path: str | Path,
    *,
    for_creation: bool = True,
    check_referential_integrity: bool = True,
) -> AssetValidationReport:
    """Validate every transformed-output asset under ``path``.

    Walks the NDJSON (``*.json``) files once. For each record it decodes the
    concrete pyatlan_v9 asset and runs :func:`validate_asset` (pass 1). When
    ``check_referential_integrity`` is set, that same walk records two things —
    the compound ``(typeName, qualifiedName)`` of every emitted **asset**, and
    the compound key of every asset **referenced by a relationship** — and a
    second pass then flags every referenced target that is not itself present in
    the batch (the orphan / dangling-parent case). Relationships are discovered
    from the data, not a hard-coded list. **Every line is always scanned** — the
    report reflects the full batch, not a sample.

    Args:
        path: A transformed-output directory (e.g. ``.../transformed``) or file.
        for_creation: Passed through to each asset's ``.validate()``.
        check_referential_integrity: Run the referential second pass.

    Returns:
        An :class:`AssetValidationReport` aggregating all axes of failure.
    """
    report = AssetValidationReport()
    referential = check_referential_integrity
    present: SpillableDict | None = None
    referenced: SpillableDict | None = None
    if referential:
        try:
            present = SpillableDict()
            referenced = SpillableDict()
        except ImportError:
            # rocksdict is an optional (``[storage]``) dependency and its absence
            # is benign — no traceback needed. We fall back to per-asset
            # validation only; the warning below (outside the except so the
            # ImportError stack isn't logged) tells the caller the orphan pass
            # was skipped.
            referential = False
            if present is not None:
                present.close()
                present = None
        except Exception:
            # A non-ImportError while allocating the spill-backed maps (e.g. the
            # second allocation fails after the first succeeded) must not leak the
            # first map's temp dir: close what was allocated, then re-raise.
            if present is not None:
                present.close()
                present = None
            if referenced is not None:
                referenced.close()
                referenced = None
            raise
    if check_referential_integrity and not referential:
        logger.warning(
            "rocksdict unavailable — skipping referential-integrity (orphan) "
            "validation; per-asset validation still runs"
        )

    try:
        for file_path, line_no, raw in iter_ndjson_lines(path):
            report.total += 1
            try:
                asset = _deserialize(raw)
            except Exception as exc:  # noqa: BLE001 — any decode failure is a data defect, not a crash
                report.undeserializable += 1
                report.failures.append(
                    AssetValidationFailure(
                        file=file_path,
                        line=line_no,
                        type_name="",
                        qualified_name="",
                        errors=[f"could not deserialize as an Atlan asset: {exc}"],
                        deserialize_error=True,
                    )
                )
                continue

            type_name = _usable_str(getattr(asset, "type_name", None)) or ""
            qualified_name = _usable_str(getattr(asset, "qualified_name", None)) or ""

            errors = validate_asset(asset, for_creation=for_creation)
            if errors:
                report.failures.append(
                    AssetValidationFailure(
                        file=file_path,
                        line=line_no,
                        type_name=type_name,
                        qualified_name=qualified_name,
                        errors=errors,
                    )
                )
            else:
                report.passed += 1

            if referential and present is not None and referenced is not None:
                if type_name and qualified_name:
                    present[_compound_key(type_name, qualified_name)] = True
                # Record every relationship target, deduped by target key: keep a
                # representative referencing asset and count the references so a
                # single missing parent is reported once, not once per child.
                for rel_name, target_tn, target_qn in _iter_relationship_refs(asset):
                    target_key = _compound_key(target_tn, target_qn)
                    existing = referenced.get(target_key)
                    if existing is None:
                        referenced[target_key] = (
                            target_tn,
                            target_qn,
                            1,
                            file_path,
                            line_no,
                            type_name,
                            qualified_name,
                            rel_name,
                        )
                    else:
                        referenced[target_key] = (
                            existing[:2] + (existing[2] + 1,) + existing[3:]
                        )

        # Pass 2: the present-set is complete, so cross-validate every referenced
        # target against it regardless of emit order within the batch.
        if referential and present is not None and referenced is not None:
            for target_key in referenced.keys():
                if target_key in present:
                    continue
                (
                    target_tn,
                    target_qn,
                    count,
                    file_path,
                    line_no,
                    type_name,
                    qualified_name,
                    rel_name,
                ) = referenced[target_key]
                report.orphans.append(
                    ReferentialFailure(
                        missing_type_name=target_tn,
                        missing_qualified_name=target_qn,
                        reference_count=count,
                        file=file_path,
                        line=line_no,
                        type_name=type_name,
                        qualified_name=qualified_name,
                        relationship=rel_name,
                    )
                )
    finally:
        if referenced is not None:
            referenced.close()
        if present is not None:
            present.close()

    return report


# ---------------------------------------------------------------------------
# The wrapper cell: NDJSON x ModelSource (ADR-0020, FND-690)
# ---------------------------------------------------------------------------
#
# Everything above predates the wrapper and is unchanged. Everything below is the
# same check reached through the generic seam: ``validate_transformed_dir`` *is* the
# NDJSON x ``ModelSource`` cell, so this is a projection, not a second scan. One
# walk, one set of findings, two report vocabularies over it.
#
# Why two vocabularies rather than one: the shared ``ArtifactValidationReport``
# speaks in units, declared fields and failure kinds, while ``ASSET_VALIDATION_EVENT``
# shipped speaking in assets, orphans and undeserializable records. That event's
# name and every attribute key are a contract — dashboards and alert rules key off
# the exact strings and v3 has shipped consumers — so the asset vocabulary is kept
# verbatim and carried *alongside* the shared shape rather than translated into it.


def supports_asset_model(model: object) -> bool:
    """True when ``model`` is a class this cell can decode NDJSON records into.

    The cell delegates to ``pyatlan_v9``'s per-asset ``.validate()`` and decodes
    through ``from_atlas_json``, both specific to the asset model — so an arbitrary
    class exposing a callable ``validate`` is **not** something this cell can check,
    however well-shaped it looks to
    :class:`~application_sdk.validation.sources.ModelSource`.

    Answering ``False`` there is what turns "we cannot check this" into a reported
    ``unsupported`` outcome naming the cell. The alternative is worse in a specific
    way: decoding every record with the wrong decoder would report a whole artifact
    as ``undecodable``, which reads as a data defect and blames the app for the
    SDK's inability to delegate.
    """
    return isinstance(model, type) and issubclass(model, Asset)


@dataclass
class AssetArtifactReport(ArtifactValidationReport):
    """The NDJSON x ``ModelSource`` cell's report: the shared shape plus asset detail.

    An :class:`~application_sdk.validation.artifacts.ArtifactValidationReport`, so
    the wrapper stamps it and every generic consumer reads it unchanged — with the
    scan's own :class:`AssetValidationReport` carried on :attr:`assets` for the two
    surfaces that speak the asset vocabulary: the shipped ``ASSET_VALIDATION_EVENT``
    attribute map and its human-readable ``format_report()`` body.

    The detail is *carried*, not re-derived, which is what makes the event
    byte-identical across the fold-in by construction:
    :func:`asset_validation_event_fields` is handed the very object the pre-wrapper
    hook emitted from.

    The shared counts and :attr:`assets` are populated together in
    :func:`validate_assets_as_artifact`, off one walk — they cannot disagree.
    """

    assets: AssetValidationReport = field(default_factory=AssetValidationReport)
    """The scan's findings in the asset vocabulary (per-asset failures + orphans)."""


def _as_record_failure(failure: AssetValidationFailure) -> ArtifactValidationFailure:
    """Project one per-asset finding onto the shared failure shape.

    ``deserialize_error`` becomes ``undecodable`` and everything else becomes
    ``invalid`` — the same split the matrix makes, so the shared report's derived
    ``undecodable`` count *equals* ``AssetValidationReport.undeserializable`` rather
    than approximating it.

    ``field`` stays "" and the messages are carried verbatim. The shared vocabulary
    addresses a unit positionally (``file:line``) and has no room for an asset's
    ``(typeName, qualifiedName)``; rewriting the model's own ``.validate()``
    messages to smuggle it in would make the two surfaces disagree about what the
    model said. The identity is on :attr:`AssetArtifactReport.assets` instead.
    """
    return ArtifactValidationFailure(
        kind="undecodable" if failure.deserialize_error else "invalid",
        file=failure.file,
        line=failure.line,
        errors=list(failure.errors),
    )


def _as_reference_failure(orphan: ReferentialFailure) -> ArtifactValidationFailure:
    """Project one orphan onto the shared failure shape.

    ``missing`` is the shared vocabulary's word for it, and ``field`` carries the
    relationship attribute the reference came through — a relationship *is* a field
    of the referencing record, so this is the same "which field" question the
    field-map cell answers, asked of a reference.

    An orphan does not move ``passed``: a record can be perfectly valid on its own
    terms and still point at a parent nobody emitted, which is exactly why the
    referential pass is a second axis. So this lands in ``failures`` (a problem
    count) without changing ``failed`` (a unit count) — the divergence the shared
    report already documents.
    """
    return ArtifactValidationFailure(
        kind="missing",
        field=orphan.relationship,
        file=orphan.file,
        line=orphan.line,
        errors=[
            f"[{orphan.missing_type_name}] {orphan.missing_qualified_name} is "
            f"referenced through '{orphan.relationship}' by "
            f"{orphan.reference_count} asset(s) in this batch but is not itself "
            f"present"
        ],
    )


def validate_assets_as_artifact(
    path: str | Path,
    declaration: ModelDeclaration | None = None,
    *,
    for_creation: bool = True,
    check_referential_integrity: bool = True,
) -> AssetArtifactReport:
    """The NDJSON x ``ModelSource`` cell: :func:`validate_transformed_dir`, reported
    in the shared shape.

    Reached through :func:`~application_sdk.validation.wrapper.validate_artifact`
    with a :class:`~application_sdk.validation.sources.ModelSource`; the
    :class:`~application_sdk.validation.ndjson.NdjsonValidator` dispatches a model
    declaration here. Callable directly too, which is how the projection is tested
    without standing up the wrapper.

    Args:
        path: A transformed-output directory (e.g. ``.../transformed``) or file.
        declaration: The resolved model declaration. Accepted for the
            :class:`~application_sdk.validation.protocols.FormatValidator` shape and
            for the model check; ``None`` skips that check, for a direct caller with
            no declaration to hand.
        for_creation: Passed through to each asset's ``.validate()``.
        check_referential_integrity: Run the referential (orphan) second pass.
            Defaults on, as the upload hook has always run it: extracts and
            transforms are full by design, so the batch is complete and the pass is
            accurate.

    Returns:
        An :class:`AssetArtifactReport`. ``absent`` when the scan found no records at
        all — the same answer the field-map arm gives, because "zero records checked"
        reported as ``clean`` is the exact failure this capability exists to remove.
        :func:`asset_validation_event_fields` still projects that to ``clean`` on the
        legacy event, whose outcome vocabulary shipped as clean/flagged only.
    """
    if declaration is not None and not supports_asset_model(declaration.model):
        # Unreachable through the wrapper, which honours ``supports``. Guarded for
        # the direct caller: decoding with the wrong decoder would report the whole
        # artifact as a data defect.
        name = getattr(declaration.model, "__name__", type(declaration.model).__name__)
        return AssetArtifactReport(
            verdict=OUTCOME_ABSENT,
            reason=(
                f"the ndjson x model cell delegates to pyatlan_v9 assets; "
                f"{name} is not one"
            ),
        )

    assets = validate_transformed_dir(
        path,
        for_creation=for_creation,
        check_referential_integrity=check_referential_integrity,
    )

    if assets.total == 0:
        return AssetArtifactReport(
            verdict=OUTCOME_ABSENT,
            reason=f"no ndjson records found at {path}",
            assets=assets,
        )

    return AssetArtifactReport(
        total=assets.total,
        passed=assets.passed,
        failures=(
            [_as_record_failure(f) for f in assets.failures]
            + [_as_reference_failure(o) for o in assets.orphans]
        ),
        assets=assets,
    )


# ---------------------------------------------------------------------------
# The shipped ASSET_VALIDATION_EVENT projection
# ---------------------------------------------------------------------------
#
# Moved here verbatim from ``app.base`` by FND-690, so the event and the check that
# feeds it live in one module. **Nothing about the emitted row changed in that
# move** — same event name, same attribute keys, same row keys and key order inside
# the matrix, same caps. ``tests/unit/validation/test_asset_event.py`` pins all of
# it against a golden payload precisely so a later refactor cannot quietly reword a
# string a dashboard matches on.

ASSET_VALIDATION_MATRIX_ERROR_MAXLEN: Final = 300
"""Per-row error message cap (chars). pyatlan_v9 ``.validate()`` messages can be
long; truncate so a single row cannot bloat the ClickHouse attribute."""


def asset_validation_matrix_json(
    report: AssetValidationReport,
    *,
    max_items: int = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
) -> str:
    """Compact per-failure matrix for the outcome event, as one JSON string.

    Lands as a single ``LogAttributes`` value in ClickHouse so connector-pulse can
    ``JSONExtract`` the per-failure detail against workflow outcomes with no schema
    change (mirrors the preflight gate's ``check_matrix``). Small fixed fields only,
    bounded to ``max_items`` rows **per axis** — the headline counts on the event
    carry the full totals, and the human-readable ``format_report()`` still rides in
    the WARNING body for flagged runs.

    Not internally guarded (``orjson.dumps`` can raise): the emitting hook wraps the
    whole emit in ``try/except``, so a raise here is caught there and never blocks
    the handoff.
    """
    rows: list[dict[str, object]] = []
    for f in report.failures[:max_items]:
        rows.append(
            {
                "kind": "undeserializable" if f.deserialize_error else "invalid",
                "type_name": f.type_name,
                "qualified_name": f.qualified_name,
                "error": (f.errors[0] if f.errors else "")[
                    :ASSET_VALIDATION_MATRIX_ERROR_MAXLEN
                ],
                "file": f.file,
                "line": f.line,
            }
        )
    for o in report.orphans[:max_items]:
        rows.append(
            {
                "kind": "orphan",
                "type_name": o.missing_type_name,
                "qualified_name": o.missing_qualified_name,
                "relationship": o.relationship,
                "reference_count": o.reference_count,
                "file": o.file,
                "line": o.line,
            }
        )
    return orjson.dumps(rows).decode()


def asset_validation_event_fields(
    report: AssetValidationReport,
    *,
    app_name: str,
    max_items: int = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
) -> dict[str, str | int]:
    """Build ``ASSET_VALIDATION_EVENT``'s attribute map from an asset report.

    The single mapping site from report to telemetry, so the emitter cannot drift
    from the allowlist — every key returned here is in
    ``logger_adaptor._KNOWN_EXTRA_KEYS``, and a key absent from that allowlist is
    dropped by ``_build_extra_dict`` and silently never reaches OTLP.

    **Every string here is a shipped contract.** The keys, the two ``outcome``
    values and the matrix's own row keys are matched verbatim by dashboards and
    alert rules, so this map is pinned against a golden payload in
    ``tests/unit/validation/test_asset_event.py`` rather than merely exercised.

    Two details are load-bearing and deliberately preserved from the pre-wrapper
    emitter:

    * ``assets_invalid`` is reported **disjointly** from
      ``assets_undeserializable``. ``AssetValidationReport.failed`` counts the
      undeserializable records too, so they are subtracted out — matching the
      headline in ``format_report()``.
    * ``outcome`` is ``clean``/``flagged`` and nothing else. The shared artifact
      report has five outcomes, but this event shipped with two, and widening the
      vocabulary of a field dashboards group by is a breaking change dressed as an
      improvement. A scan that found nothing to read reports ``clean`` here and
      ``absent`` on the shared report.

    Args:
        report: The scan's findings in the asset vocabulary — i.e.
            :attr:`AssetArtifactReport.assets`.
        app_name: Emitting app, carried as the already-allowlisted ``app_name``.
        max_items: Row cap per axis for the matrix; shared with ``format_report``.

    Returns:
        Keyword arguments for the ``ASSET_VALIDATION_EVENT`` log call.
    """
    return {
        "outcome": "clean" if report.ok else "flagged",
        "app_name": app_name,
        "assets_total": report.total,
        "assets_passed": report.passed,
        "assets_invalid": report.failed - report.undeserializable,
        "assets_orphaned": len(report.orphans),
        "assets_undeserializable": report.undeserializable,
        ASSET_VALIDATION_MATRIX_KEY: asset_validation_matrix_json(
            report, max_items=max_items
        ),
    }

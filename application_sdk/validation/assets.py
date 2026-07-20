"""Asset-write validation built on the pyatlan_v9 ``.validate()`` backbone.

See :mod:`application_sdk.validation` for the why. This module holds the typed
result models, the single-asset check, and the transformed-output directory
walk (per-asset validation + referential-integrity second pass).

Design constraints worth preserving if you edit this file:

* **msgspec Structs only on the read path.** pyatlan_v9 assets *are*
  ``msgspec.Struct`` instances; ``<ConcreteType>.from_json(bytes)`` decodes the
  NDJSON line straight into the concrete typed Struct. We never build an
  intermediate ``dict`` — the only decode besides the concrete one is a tiny
  reusable probe that reads just ``typeName`` so we can pick the concrete class.
* **Concrete type resolution is load-bearing.** ``Asset.from_json`` alone yields a
  *generic* ``Asset`` whose ``.validate()`` is only the 3-field base check and
  which drops every typed relationship attribute. Resolving the concrete class via
  :func:`pyatlan_v9.model.transform.get_type` is what unlocks the rich
  ``for_creation`` hierarchy checks and the typed parent relationships the
  referential pass reads.
* **Relationships are discovered, not hard-coded.** The referential pass reads
  whatever relationship references each asset actually carries in the NDJSON
  (enumerated generically off the Struct's relationship-typed fields) and
  cross-validates that every referenced ``(typeName, qualifiedName)`` also
  appears as an emitted asset. There is deliberately no per-type parent map —
  Atlan has hundreds of relationships and the set changes constantly.
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
from glob import glob
from pathlib import Path
from typing import Iterator

import msgspec
from pyatlan_v9.model.assets import Asset
from pyatlan_v9.model.assets.referenceable import RelatedReferenceable
from pyatlan_v9.model.transform import get_type

from application_sdk.common.spillable_dict import SpillableDict
from application_sdk.constants import ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
from application_sdk.observability.logger_adaptor import get_logger

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
# Deserialization (msgspec Structs only — no dict materialization)
# ---------------------------------------------------------------------------


class _TypeProbe(msgspec.Struct, rename="camel", forbid_unknown_fields=False):
    """Minimal decode target used only to sniff ``typeName`` off a raw line."""

    type_name: str = ""


_TYPE_PROBE_DECODER = msgspec.json.Decoder(_TypeProbe)


def _deserialize(raw: bytes) -> Asset:
    """Decode one NDJSON line into its concrete pyatlan_v9 asset Struct.

    Raises whatever msgspec/pyatlan raise on malformed input — callers convert
    that into an ``undeserializable`` count rather than letting it abort a batch.
    """
    type_name = _TYPE_PROBE_DECODER.decode(raw).type_name
    asset_cls = get_type(type_name) if type_name else Asset
    return asset_cls.from_json(raw)


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


def _iter_ndjson_lines(path: str | Path) -> Iterator[tuple[str, int, bytes]]:
    """Yield ``(file, 1-based line number, raw bytes)`` for every non-blank line.

    Accepts a directory (walked recursively for ``*.json``, sorted for stable
    ordering) or a single file. A missing path yields nothing.
    """
    root = Path(path)
    if root.is_dir():
        files = sorted(glob(str(root / "**" / "*.json"), recursive=True))
    elif root.is_file():
        files = [str(root)]
    else:
        files = []
    for file_path in files:
        with open(file_path, "rb") as handle:
            for line_no, raw in enumerate(handle, start=1):
                stripped = raw.strip()
                if stripped:
                    yield file_path, line_no, stripped


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
        for file_path, line_no, raw in _iter_ndjson_lines(path):
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

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
* **Bounded memory.** The referential pass keys on the compound
  ``(typeName, qualifiedName)`` and spills the present-set and the parent-lookup
  queue to disk via :class:`~application_sdk.common.spillable_dict.SpillableDict`,
  so a multi-million-asset batch never blows the heap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Iterator

import msgspec
from pyatlan_v9.model.assets import Asset
from pyatlan_v9.model.transform import get_type

from application_sdk.common.spillable_dict import SpillableDict
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


@dataclass(frozen=True)
class ReferentialFailure:
    """A child asset whose parent ``(typeName, qualifiedName)`` is absent from the batch."""

    file: str
    line: int
    type_name: str
    qualified_name: str
    missing_parent_type_name: str
    missing_parent_qualified_name: str


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

    def format_report(self, *, max_items: int = 25) -> str:
        """Render a human-readable summary.

        ``max_items`` caps how many failures/orphans are *listed*, never how many
        are examined — the headline counts always reflect the full batch.
        """
        lines = [
            f"Asset validation: {self.passed}/{self.total} passed, "
            f"{self.failed} invalid, {len(self.orphans)} orphaned, "
            f"{self.undeserializable} undeserializable"
        ]
        for failure in self.failures[:max_items]:
            loc = _location(failure.file, failure.line)
            label = failure.qualified_name or "<no qualifiedName>"
            lines.append(
                f"  INVALID [{failure.type_name or '?'}] {label}{loc}: "
                + "; ".join(failure.errors)
            )
        extra_failures = len(self.failures) - max_items
        if extra_failures > 0:
            lines.append(f"  ... and {extra_failures} more invalid assets")
        for orphan in self.orphans[:max_items]:
            loc = _location(orphan.file, orphan.line)
            lines.append(
                f"  ORPHAN [{orphan.type_name}] {orphan.qualified_name}{loc}: "
                f"parent [{orphan.missing_parent_type_name}] "
                f"{orphan.missing_parent_qualified_name} not present in batch"
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
# Compound-key encoding + parent resolution
# ---------------------------------------------------------------------------

_KEY_SEP = "\x00"
"""Separator for the ``(typeName, qualifiedName)`` compound key. NUL never
appears in a typeName or qualifiedName, so the encoding is unambiguous."""


def _compound_key(type_name: str, qualified_name: str) -> str:
    return f"{type_name}{_KEY_SEP}{qualified_name}"


# Hierarchical parent relationship attributes per Atlas ``typeName``, in priority
# order. Only parents that are expected to appear in the SAME transformed batch
# belong here — a type absent from this map is treated as parent-less, so we never
# raise a false-positive orphan (e.g. Database's parent Connection is created out
# of band and is intentionally omitted). The parent's typeName is read from the
# related object itself, so it is never hard-coded here. Extend as more asset
# families adopt referential validation.
_PARENT_RELATIONSHIP_ATTRS: dict[str, tuple[str, ...]] = {
    "Schema": ("database",),
    "Table": ("atlan_schema",),
    "View": ("atlan_schema",),
    "MaterialisedView": ("atlan_schema",),
    "TablePartition": ("table",),
    "Column": (
        "table",
        "view",
        "materialised_view",
        "table_partition",
        "calculation_view",
    ),
}


def _usable_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty ``str``, else ``None``.

    pyatlan_v9 marks unset fields with an ``UNSET`` sentinel (not ``None``), so a
    concrete ``str`` check is the sentinel-agnostic way to ask "is this set?".
    """
    return value if isinstance(value, str) and value else None


def _parent_key(asset: Asset, type_name: str) -> tuple[str, str] | None:
    """Return the parent ``(typeName, qualifiedName)`` for ``asset``.

    ``None`` when the type has no in-batch parent or no parent reference is set
    (the latter is a per-asset ``for_creation`` concern, caught by pass 1).
    """
    for attr in _PARENT_RELATIONSHIP_ATTRS.get(type_name, ()):
        related = getattr(asset, attr, None)
        if related is None:
            continue
        parent_qn = _usable_str(getattr(related, "qualified_name", None))
        parent_tn = _usable_str(getattr(related, "type_name", None))
        if parent_qn and parent_tn:
            return parent_tn, parent_qn
    return None


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
    except ValueError as exc:
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
    ``check_referential_integrity`` is set, it also records each asset's compound
    ``(typeName, qualifiedName)`` key and, after the walk, flags any child whose
    referenced parent key is absent from the batch (pass 2). **Every line is
    always scanned** — the report reflects the full batch, not a sample.

    Args:
        path: A transformed-output directory (e.g. ``.../transformed``) or file.
        for_creation: Passed through to each asset's ``.validate()``.
        check_referential_integrity: Run the orphan-detection second pass.

    Returns:
        An :class:`AssetValidationReport` aggregating all axes of failure.
    """
    report = AssetValidationReport()
    referential = check_referential_integrity
    present: SpillableDict | None = None
    parent_lookups: SpillableDict | None = None
    if referential:
        try:
            present = SpillableDict()
            parent_lookups = SpillableDict()
        except ImportError:
            # rocksdict is an optional (``[storage]``) dependency. Without it we
            # can still run per-asset validation — only the cross-record orphan
            # pass is skipped.
            logger.warning(
                "rocksdict unavailable — skipping referential-integrity (orphan) "
                "validation; per-asset validation still runs"
            )
            referential = False
            if present is not None:
                present.close()
                present = None

    try:
        lookup_index = 0
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

            if referential and present is not None and parent_lookups is not None:
                if type_name and qualified_name:
                    present[_compound_key(type_name, qualified_name)] = True
                parent = _parent_key(asset, type_name)
                if parent is not None:
                    parent_lookups[lookup_index] = (
                        file_path,
                        line_no,
                        type_name,
                        qualified_name,
                        parent[0],
                        parent[1],
                    )
                    lookup_index += 1

        # Pass 2: the present-set is complete now, so parent membership is safe to
        # check regardless of child/parent ordering within the batch.
        if referential and present is not None and parent_lookups is not None:
            for (
                file_path,
                line_no,
                type_name,
                qualified_name,
                parent_tn,
                parent_qn,
            ) in parent_lookups.values():
                if _compound_key(parent_tn, parent_qn) not in present:
                    report.orphans.append(
                        ReferentialFailure(
                            file=file_path,
                            line=line_no,
                            type_name=type_name,
                            qualified_name=qualified_name,
                            missing_parent_type_name=parent_tn,
                            missing_parent_qualified_name=parent_qn,
                        )
                    )
    finally:
        if parent_lookups is not None:
            parent_lookups.close()
        if present is not None:
            present.close()

    return report

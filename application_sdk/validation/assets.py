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
* **A report that does not grow with the failure count.** The upload-time cell
  keeps at most ``ASSET_VALIDATION_MAX_ITEMS_PER_AXIS`` failures and orphans as
  objects and counts the rest, tallying the per-rule summary as it walks. Every
  consumer lists no more rows than that, so nothing it reads changes, while the
  report the scan's child pickles back stays a few KB on a batch where every
  record fails. The summary tallies grow only with distinct
  ``(kind, typeName, rule)`` keys, which the type model bounds for real output.
  One object per failure would grow with the failure count three times over: in
  the child, in the pickle and in the parent that unpickles it.
* **The walk polices its own budget.** Every ``_BUDGET_CHECK_EVERY`` records it
  checks how far its RSS has grown and how long it has run, and past either
  budget it stops and reports what it read, marked ``truncated``. The wall
  budget starts when the walk does, so time spent queued behind a busy process
  pool or spawning the child counts only against the caller's outer timeout,
  which can still fire first under that contention.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import re
import sys
import time
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterator, Sequence

import msgspec
import orjson
from pyatlan_v9.model.assets import Asset
from pyatlan_v9.model.assets.referenceable import RelatedReferenceable
from pyatlan_v9.model.transform import from_atlas_json

from application_sdk.common.spillable_dict import SpillableDict
from application_sdk.constants import ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
from application_sdk.contracts.types import (
    CONNECTION_QN_MIN_SEGMENTS,
    connection_qualified_name_defect,
)
from application_sdk.observability.logger_adaptor import (
    ASSET_VALIDATION_MATRIX_KEY,
    ASSET_VALIDATION_SUMMARY_KEY,
    ASSET_VALIDATION_TRUNCATED_KEY,
    get_logger,
)
from application_sdk.validation.artifacts import (
    OUTCOME_ABSENT,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    ModelDeclaration,
)
from application_sdk.validation.asset_rules import (
    AssetCreationRules,
    creation_rule_errors,
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
    decode_reason: str = ""
    """Value-free class of a decode failure — ``malformed_json``,
    ``schema_mismatch:<path>`` or the exception class name. Empty unless
    ``deserialize_error``."""


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


REFERENTIAL_CHECK_RAN: Final = "ran"
REFERENTIAL_CHECK_NOT_REQUESTED: Final = "not_requested"
REFERENTIAL_CHECK_SKIPPED_UNAVAILABLE: Final = "skipped_unavailable"
#: The scan stopped at its memory or wall budget before the orphan pass finished.
#: Stopped during the record walk, the present-set is incomplete, so no orphan is
#: reported; stopped during the orphan pass, the orphans reported are real but
#: there may be more.
REFERENTIAL_CHECK_SKIPPED_BUDGET: Final = "skipped_budget"
#: A report not produced by the walk (e.g. built by hand) cannot say.
REFERENTIAL_CHECK_UNKNOWN: Final = "unknown"

#: :attr:`AssetValidationReport.truncated` values: which budget stopped the scan.
TRUNCATED_RSS_BUDGET: Final = "rss_budget"
TRUNCATED_WALL_BUDGET: Final = "wall_budget"

_SummaryKey: typing.TypeAlias = tuple[str, str, str]
"""``(kind, type_name, detail)`` — one ``asset_validation_summary`` row."""


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
    """Per-asset validation failures (includes deserialize failures), in batch
    order. The first ``max_samples`` only when the walk was given a cap; the rest
    are counted in :attr:`failures_omitted`."""
    orphans: list[ReferentialFailure] = field(default_factory=list)
    """Referential-integrity failures from the second pass, in key order. Capped
    like :attr:`failures`; the rest are counted in :attr:`orphans_omitted`."""
    referential_check: str = REFERENTIAL_CHECK_UNKNOWN
    """Whether the orphan pass ran: ``ran``, ``not_requested`` (the caller turned
    it off, e.g. an incomplete fan-in batch), ``skipped_unavailable`` (no spill
    store) or ``skipped_budget`` (the scan stopped at a budget first). Zero orphans
    means "none found" only when this is ``ran``."""
    failures_omitted: int = 0
    """Failures counted but not kept in :attr:`failures`, past the sample cap."""
    orphans_omitted: int = 0
    """Orphans counted but not kept in :attr:`orphans`, past the sample cap."""
    truncated: str = ""
    """Empty when every record was read. Otherwise the budget that stopped the scan
    (:data:`TRUNCATED_RSS_BUDGET` or :data:`TRUNCATED_WALL_BUDGET`), and every count
    covers only the records read before it."""
    summary_counts: dict[_SummaryKey, int] | None = None
    """Per-``(kind, type_name, detail)`` failure counts over the whole batch, tallied
    by the walk so ``asset_validation_summary`` stays exact when the lists are
    capped. ``None`` on a report not built by the walk, whose lists are then the
    record the summary is derived from."""
    summary_references: dict[_SummaryKey, int] | None = None
    """Per-orphan-group reference totals, tallied alongside :attr:`summary_counts`."""

    @property
    def ok(self) -> bool:
        """True when nothing failed on any axis."""
        return self.failed == 0 and self.orphaned == 0 and self.undeserializable == 0

    @property
    def failed(self) -> int:
        """Count of per-asset validation failures (includes undeserializable)."""
        return len(self.failures) + self.failures_omitted

    @property
    def orphaned(self) -> int:
        """Count of referenced targets absent from the batch."""
        return len(self.orphans) + self.orphans_omitted

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
            f"{invalid} invalid, {self.orphaned} orphaned, "
            f"{self.undeserializable} undeserializable"
        ]
        if self.truncated:
            lines.append(
                f"  scan stopped at its {_TRUNCATED_LABELS.get(self.truncated, self.truncated)} "
                f"after {self.total} records; the counts cover those records only"
            )
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
        if self.failures_omitted:
            # The samples are the head of the batch; the counters cover the tail.
            undeser_omitted = self.undeserializable - sum(
                1 for f in self.failures if f.deserialize_error
            )
            extra_undeser += undeser_omitted
            extra_invalid += self.failures_omitted - undeser_omitted
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
        extra_orphans = self.orphaned - len(self.orphans[:max_items])
        if extra_orphans > 0:
            lines.append(f"  ... and {extra_orphans} more orphans")
        return "\n".join(lines)


_TRUNCATED_LABELS: Final = {
    TRUNCATED_RSS_BUDGET: "rss budget",
    TRUNCATED_WALL_BUDGET: "wall budget",
}


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
# Connection scoping of qualifiedNames (FND-3523)
# ---------------------------------------------------------------------------
#
# Every connection-scoped Atlas qualifiedName starts ``default/<connector>/<id>``.
# A connector that builds one without that prefix — or points at a same-connection
# parent it never emits — gets ``ATLAS-404-00-00A`` from publish, on every run, until
# the connector is fixed. pyatlan's per-type patterns catch neither: most types have
# no pattern, and the ones that do check segment counts, not the prefix.


def _is_connection_scoped(qualified_name: str) -> bool:
    """True when ``qualified_name`` is a connection, or sits under one.

    Its first three segments must form a well-formed connection qualifiedName by
    :func:`~application_sdk.contracts.types.connection_qualified_name_defect` — the
    one definition every reader shares. That rule accepts a non-numeric ``<id>``
    (programmatically provisioned connections carry names there), so this must too:
    a stricter local pattern would fail references those connections publish fine.
    """
    head = qualified_name.split("/", CONNECTION_QN_MIN_SEGMENTS)
    connection = "/".join(head[:CONNECTION_QN_MIN_SEGMENTS])
    return connection_qualified_name_defect(connection) is None


_UNSCOPED_TYPE_NAMES: Final = frozenset(
    {
        "AtlasGlossary",
        "AtlasGlossaryCategory",
        "AtlasGlossaryTerm",
        "AuthPolicy",
        "Badge",
        "DataDomain",
        "DataProduct",
        "Link",
        "Persona",
        "Purpose",
        "Readme",
        "Stakeholder",
        "StakeholderTitle",
    }
)
"""Types whose qualifiedName is not under a connection, so a reference to one is
exempt from the prefix check. An allowlist on purpose: a new exception is added
here when it turns up, rather than inferred from the type model."""

_CONNECTION_PREFIX_EXAMPLE: Final = "default/<connector>/<id>/"


def _connection_prefix_errors(
    qualified_name: str,
    connection_qualified_name: str,
    type_name: str,
    refs: Sequence[tuple[str, str, str]],
) -> list[str]:
    """Messages for qualifiedNames that do not carry their connection prefix.

    * The asset's own ``qualifiedName`` must start ``connectionQualifiedName + "/"``.
      Only checked when the record states its connection — a record without one
      (a relationship-only update, a glossary term) has nothing to compare against.
    * Every reference qualifiedName must be well-formed and connection-scoped. It
      may point at another connection (cross-connection lineage); only the shape is
      checked here. References carrying no qualifiedName (guid- or ARS-identity
      references) never reach this — :func:`_iter_relationship_refs` skips them.

    Reference failures are reported once per relationship field, with a count, so an
    asset carrying thousands of malformed list entries yields one message per field.
    """
    errors: list[str] = []
    if (
        connection_qualified_name
        and qualified_name
        and type_name != "Connection"
        and not qualified_name.startswith(connection_qualified_name + "/")
    ):
        errors.append(
            f"qualified_name '{qualified_name}' lacks its connection prefix "
            f"'{connection_qualified_name}/'"
        )
    bad_refs: dict[str, list[str]] = {}
    for rel_name, target_tn, target_qn in refs:
        if target_tn in _UNSCOPED_TYPE_NAMES or _is_connection_scoped(target_qn):
            continue
        bad_refs.setdefault(rel_name, []).append(target_qn)
    for rel_name, target_qns in bad_refs.items():
        more = f" (and {len(target_qns) - 1} more)" if len(target_qns) > 1 else ""
        errors.append(
            f"{rel_name} reference '{target_qns[0]}' lacks its connection prefix "
            f"'{_CONNECTION_PREFIX_EXAMPLE}'{more}"
        )
    return errors


def _is_same_connection(target_qn: str, connection_qualified_name: str) -> bool:
    """True when the referential pass must find ``target_qn`` in the batch.

    A reference under the referrer's own connection points at something this run
    should have emitted. Anything else — cross-connection lineage, the connection
    itself, an unscoped type such as a glossary term — exists outside this output,
    so its absence here is not an orphan. A referrer that states no connection keeps
    the original behaviour: every reference is checked.
    """
    if not connection_qualified_name:
        return True
    return target_qn.startswith(connection_qualified_name + "/")


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


class _TypeNameProbe(msgspec.Struct):
    """Reads only ``typeName`` — every other key is skipped, not decoded."""

    typeName: str = ""


_TYPE_NAME_PROBE = msgspec.json.Decoder(_TypeNameProbe)
_VALIDATION_ERROR_PATH = re.compile(r" - at `\$([^`]*)`\Z")
_PATH_INDEX = re.compile(r"\[\d+\]")


def _probe_type_name(raw: bytes) -> str:
    """Best-effort ``typeName`` of a record the full decode rejected, so a decode
    failure still attributes to an asset type. ``""`` when even that is unreadable."""
    try:
        return _TYPE_NAME_PROBE.decode(raw).typeName
    except (msgspec.DecodeError, msgspec.ValidationError):
        return ""  # conformance: ignore[E007] the full decode failure is already recorded; "" is the documented "type unknown" value


def _decode_reason(exc: BaseException) -> str:
    """Classify a decode failure without its record values.

    msgspec's ``ValidationError`` names the offending path (``... - at
    `$.columnCount```); list indexes collapse to ``[]`` so the key is per field,
    not per element.
    """
    if isinstance(exc, msgspec.ValidationError):
        match = _VALIDATION_ERROR_PATH.search(str(exc))
        if match is None:
            return "schema_mismatch"
        path = _PATH_INDEX.sub("[]", match.group(1)).lstrip(".")
        return f"schema_mismatch:{path}" if path else "schema_mismatch"
    if isinstance(exc, msgspec.DecodeError):
        return "malformed_json"
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Memory and wall budgets
# ---------------------------------------------------------------------------

_BUDGET_CHECK_EVERY: Final = 1024
"""Records (or orphan-pass keys) between two budget checks. A check is one clock
read and one small ``/proc`` read, so this keeps its cost out of the profile while
reacting within a fraction of a second."""

_WALL_BUDGET_SHARE: Final = 0.9
"""Share of ``ATLAN_VALIDATE_ASSETS_TIMEOUT_SECONDS`` the scan may spend before it
stops itself. The rest covers what the outer timeout also counts — the child's
spawn and imports, and returning the report — so a slow batch ends in partial
counts instead of being killed with none."""

_MIB: Final = 1024 * 1024

_monotonic = time.monotonic


@dataclass(frozen=True)
class ScanBudget:
    """Where :func:`validate_transformed_dir` stops on its own.

    A stopped scan returns the counts for the records it read, with
    :attr:`AssetValidationReport.truncated` naming the budget. ``None`` leaves an
    axis unbounded.
    """

    max_rss_bytes: int | None = None
    """Stop once this process's resident memory has grown by more than this many
    bytes since the scan started. Growth rather than the absolute figure, so memory
    the process already held — its imports, or what a reused pool child kept from
    an earlier task — never cuts a scan short that has itself used little. The
    process's total can therefore exceed this by whatever it held at the start."""
    wall_seconds: float | None = None
    """Stop once the scan has run this long."""

    @classmethod
    def from_deployment(cls) -> ScanBudget:
        """The budgets the upload-time scan runs under, from the deployment's env:
        ``ATLAN_VALIDATE_ASSETS_MAX_RSS_MB`` of RSS growth (``0`` = no memory check)
        and 90% of ``ATLAN_VALIDATE_ASSETS_TIMEOUT_SECONDS``."""
        from application_sdk import (  # noqa: PLC0415 — read at call time so a deployment (or a test) can retune them
            constants,
        )

        rss_mb = constants.VALIDATE_ASSETS_MAX_RSS_MB
        return cls(
            max_rss_bytes=rss_mb * _MIB if rss_mb > 0 else None,
            wall_seconds=constants.VALIDATE_ASSETS_TIMEOUT_SECONDS * _WALL_BUDGET_SHARE,
        )


def _current_rss_bytes() -> int | None:
    """This process's resident memory now, its peak where "now" is not readable, or
    ``None`` where neither is (Windows, which then runs without the memory check).

    Linux (every deployed worker) reads ``/proc/self/statm``. Elsewhere the
    lifetime peak from ``getrusage`` stands in, so growth there is measured past
    the highest point the process had already reached.
    """
    try:
        import resource  # noqa: PLC0415 — POSIX-only module; absent on Windows
    except ImportError:  # conformance: ignore[E009] Windows has no cheap RSS read; the documented answer is "unknown", and the wall budget still applies
        return None
    try:
        with open("/proc/self/statm", "rb") as handle:  # noqa: ASYNC230 — runs in the scan's own child process, not on an event loop
            return int(handle.read().split()[1]) * resource.getpagesize()
    except (
        OSError,
        ValueError,
        IndexError,
    ):  # conformance: ignore[E009] no /proc off Linux; the peak below is the documented stand-in
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS and KiB on Linux and the BSDs.
        return peak if sys.platform == "darwin" else peak * 1024


class _BudgetClock:
    """Answers "has the scan used up a budget?" once every ``_BUDGET_CHECK_EVERY`` ticks."""

    __slots__ = ("_deadline", "_rss_ceiling", "_ticks")

    def __init__(self, budget: ScanBudget) -> None:
        self._deadline = (
            None if budget.wall_seconds is None else _monotonic() + budget.wall_seconds
        )
        start_rss = None if budget.max_rss_bytes is None else _current_rss_bytes()
        self._rss_ceiling = (
            None
            if budget.max_rss_bytes is None or start_rss is None
            else start_rss + budget.max_rss_bytes
        )
        self._ticks = 0

    def tick(self) -> str:
        """Call before each unit of work. Returns ``""`` to go on, or the name of the
        exhausted budget — checked once every ``_BUDGET_CHECK_EVERY`` units, so a
        stop always lands after a whole number of them."""
        if self._ticks and not self._ticks % _BUDGET_CHECK_EVERY:
            if self._deadline is not None and _monotonic() >= self._deadline:
                return TRUNCATED_WALL_BUDGET
            if (
                self._rss_ceiling is not None
                and (_current_rss_bytes() or 0) > self._rss_ceiling
            ):
                return TRUNCATED_RSS_BUDGET
        self._ticks += 1
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_asset(
    asset: Asset,
    *,
    for_creation: bool = True,
    creation_rules: frozenset[AssetCreationRules] = frozenset(),
) -> list[str]:
    """Run pyatlan_v9's ``.validate()`` and return its error messages.

    Returns an empty list when the asset is valid. Never raises — a failed
    validation surfaces as the returned messages.

    Args:
        asset: A concrete pyatlan_v9 asset instance.
        for_creation: When True (default), also enforce the create-time hierarchy
            checks. Connector runs currently assume a first-time run against a
            source, so everything they emit as transformed output is for initial
            creation.
        creation_rules: Extra create-time checks pyatlan no longer runs (see
            :mod:`application_sdk.validation.asset_rules`). Applied only with
            ``for_creation``; empty (default) keeps pyatlan's behaviour.
    """
    try:
        asset.validate(for_creation=for_creation)
    except Exception as exc:  # noqa: BLE001 — "Never raises": any validate() error surfaces as a message, never aborts the batch
        errors = [str(exc)]
    else:
        errors = []
    if for_creation and creation_rules:
        errors += creation_rule_errors(asset, creation_rules)
    return errors


def validate_transformed_dir(
    path: str | Path | Sequence[str | Path],
    *,
    for_creation: bool = True,
    check_referential_integrity: bool = True,
    creation_rules: frozenset[AssetCreationRules] = frozenset(),
    max_samples: int | None = None,
    budget: ScanBudget | None = None,
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

    Two connection-scoping checks ride the same walk (FND-3523), because a miss on
    either fails publish with ``ATLAS-404-00-00A``:

    * **Prefix** (per-asset, always on): an asset's own ``qualifiedName`` must start
      with its ``connectionQualifiedName + "/"``, and every reference qualifiedName
      must be well-formed ``default/<connector>/<id>/...`` — it may name another
      connection. A miss is an ``invalid`` failure with a ``connection_prefix:<field>``
      rule key.
    * **Same-connection references exist**: the orphan pass only checks references
      under the referrer's own connection. Cross-connection lineage and unscoped
      targets (glossary terms, the connection itself) live outside this output and
      are not orphans. Run it on a full extract: an incremental run that skips
      unchanged parents will report them.

    Args:
        path: A transformed-output directory (e.g. ``.../transformed``) or file,
            or a sequence of them read as one batch — the declared parts of a
            fanned-in hand-off. The referential pass spans the whole batch, so
            a parent emitted by one part satisfies a child in another
            (FND-3414).
        for_creation: Passed through to each asset's ``.validate()``.
        check_referential_integrity: Run the referential second pass.
        creation_rules: Extra create-time checks, passed through to
            :func:`validate_asset`.
        max_samples: Keep at most this many failures and this many orphans as
            objects on the report; count the rest. The counts, and the per-rule
            summary the walk tallies, still cover every record. ``None`` (default)
            keeps every one; a value below 1 is read as 1, so a failing batch
            always lists at least one failure.
        budget: Stop early at a memory or wall budget (see :class:`ScanBudget`),
            returning the counts for the records read so far. ``None`` (default)
            reads the whole batch whatever it costs.

    Returns:
        An :class:`AssetValidationReport` aggregating all axes of failure.
    """
    if max_samples is not None:
        max_samples = max(1, max_samples)
    summary_counts: dict[_SummaryKey, int] = {}
    summary_references: dict[_SummaryKey, int] = {}
    report = AssetValidationReport(
        summary_counts=summary_counts, summary_references=summary_references
    )
    clock = _BudgetClock(budget or ScanBudget())

    def keep_failure(failure: AssetValidationFailure) -> None:
        _tally_failure(summary_counts, failure)
        if max_samples is None or len(report.failures) < max_samples:
            report.failures.append(failure)
        else:
            report.failures_omitted += 1

    referential = check_referential_integrity
    present: SpillableDict | None = None
    referenced: SpillableDict | None = None
    if referential:
        try:
            present = SpillableDict()
            referenced = SpillableDict()
        except ImportError:
            # rocksdict is a core dependency (FND-3414), so this is reachable
            # only in an environment that removed it by hand. Fall back to
            # per-asset validation; the warning below (outside the except so the
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
    if referential:
        report.referential_check = REFERENTIAL_CHECK_RAN
    elif check_referential_integrity:
        report.referential_check = REFERENTIAL_CHECK_SKIPPED_UNAVAILABLE
    else:
        report.referential_check = REFERENTIAL_CHECK_NOT_REQUESTED
    if check_referential_integrity and not referential:
        logger.warning(
            "rocksdict unavailable — skipping referential-integrity (orphan) "
            "validation; per-asset validation still runs"
        )

    try:
        with contextlib.closing(iter_ndjson_lines(path)) as lines:
            report.truncated = _validate_records(
                lines,
                report,
                keep_failure,
                clock,
                present=present if referential else None,
                referenced=referenced if referential else None,
                for_creation=for_creation,
                creation_rules=creation_rules,
            )

        # Pass 2: the present-set is complete, so cross-validate every referenced
        # target against it regardless of emit order within the batch. Skipped when
        # the walk stopped early: against a partial present-set every parent in the
        # unread tail would read as missing.
        if report.truncated and referential:
            report.referential_check = REFERENTIAL_CHECK_SKIPPED_BUDGET
        elif referential and present is not None and referenced is not None:
            report.truncated = _find_orphans(
                report,
                present,
                referenced,
                clock,
                summary_counts=summary_counts,
                summary_references=summary_references,
                max_samples=max_samples,
            )
            if report.truncated:
                report.referential_check = REFERENTIAL_CHECK_SKIPPED_BUDGET
    finally:
        if referenced is not None:
            referenced.close()
        if present is not None:
            present.close()

    return report


def _validate_records(
    lines: Iterator[tuple[str, int, bytes]],
    report: AssetValidationReport,
    keep_failure: typing.Callable[[AssetValidationFailure], None],
    clock: _BudgetClock,
    *,
    present: SpillableDict | None,
    referenced: SpillableDict | None,
    for_creation: bool,
    creation_rules: frozenset[AssetCreationRules],
) -> str:
    """Pass 1: decode and check each record, and fill the two referential maps.

    Returns ``""`` after the last record, or the name of the budget that stopped
    the walk first.
    """
    for file_path, line_no, raw in lines:
        if exhausted := clock.tick():
            return exhausted
        report.total += 1
        try:
            asset = _deserialize(raw)
        except Exception as exc:  # noqa: BLE001 — any decode failure is a data defect, not a crash
            report.undeserializable += 1
            keep_failure(
                AssetValidationFailure(
                    file=file_path,
                    line=line_no,
                    type_name=_probe_type_name(raw),
                    qualified_name="",
                    errors=[f"could not deserialize as an Atlan asset: {exc}"],
                    deserialize_error=True,
                    decode_reason=_decode_reason(exc),
                )
            )
            continue

        type_name = _usable_str(getattr(asset, "type_name", None)) or ""
        qualified_name = _usable_str(getattr(asset, "qualified_name", None)) or ""
        connection_qn = (
            _usable_str(getattr(asset, "connection_qualified_name", None)) or ""
        )
        refs = tuple(_iter_relationship_refs(asset))

        errors = validate_asset(
            asset, for_creation=for_creation, creation_rules=creation_rules
        )
        errors += _connection_prefix_errors(
            qualified_name, connection_qn, type_name, refs
        )
        if errors:
            keep_failure(
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

        if present is not None and referenced is not None:
            if type_name and qualified_name:
                present[_compound_key(type_name, qualified_name)] = True
            # Record every relationship target, deduped by target key: keep a
            # representative referencing asset and count the references so a
            # single missing parent is reported once, not once per child.
            for rel_name, target_tn, target_qn in refs:
                if not _is_same_connection(target_qn, connection_qn):
                    continue
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
    return ""


def _find_orphans(
    report: AssetValidationReport,
    present: SpillableDict,
    referenced: SpillableDict,
    clock: _BudgetClock,
    *,
    summary_counts: dict[_SummaryKey, int],
    summary_references: dict[_SummaryKey, int],
    max_samples: int | None,
) -> str:
    """Pass 2: every referenced target absent from the present-set is an orphan.

    Streams ``referenced`` in key order. Each orphan is tallied into the summary;
    only the first ``max_samples`` become objects on the report. Returns ``""``
    when every key was checked, or the name of the budget that stopped it first.
    """
    for target_key in referenced.keys():
        if exhausted := clock.tick():
            return exhausted
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
        orphan = ReferentialFailure(
            missing_type_name=target_tn,
            missing_qualified_name=target_qn,
            reference_count=count,
            file=file_path,
            line=line_no,
            type_name=type_name,
            qualified_name=qualified_name,
            relationship=rel_name,
        )
        _tally_orphan(summary_counts, summary_references, orphan)
        if max_samples is None or len(report.orphans) < max_samples:
            report.orphans.append(orphan)
        else:
            report.orphans_omitted += 1
    return ""


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
    path: str | Path | Sequence[str | Path],
    declaration: ModelDeclaration | None = None,
    *,
    for_creation: bool = True,
    check_referential_integrity: bool = True,
    creation_rules: frozenset[AssetCreationRules] = frozenset(),
    budget: ScanBudget | None = None,
    max_samples: int | None = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
) -> AssetArtifactReport:
    """The NDJSON x ``ModelSource`` cell: :func:`validate_transformed_dir`, reported
    in the shared shape.

    Reached through :func:`~application_sdk.validation.wrapper.validate_artifact`
    with a :class:`~application_sdk.validation.sources.ModelSource`; the
    :class:`~application_sdk.validation.ndjson.NdjsonValidator` dispatches a model
    declaration here. Callable directly too, which is how the projection is tested
    without standing up the wrapper.

    Args:
        path: A transformed-output directory (e.g. ``.../transformed``) or file,
            or a sequence of them read as one batch (see
            :func:`validate_transformed_dir`).
        declaration: The resolved model declaration. Accepted for the
            :class:`~application_sdk.validation.protocols.FormatValidator` shape and
            for the model check; ``None`` skips that check, for a direct caller with
            no declaration to hand.
        for_creation: Passed through to each asset's ``.validate()``.
        check_referential_integrity: Run the referential (orphan) second pass.
            Defaults on, as the upload hook has always run it: extracts and
            transforms are full by design, so the batch is complete and the pass is
            accurate.
        creation_rules: Extra create-time checks, passed through to
            :func:`validate_transformed_dir`.
        budget: Where the scan stops early. ``None`` (default) takes the
            deployment's, :meth:`ScanBudget.from_deployment` — the upload hook
            reaches this cell through the wrapper, which carries no options, so the
            cell reads them itself.
        max_samples: Failures, and orphans, kept as objects (see below). ``None``
            keeps every one, for a caller that reads the whole list in-process.

    With the default ``max_samples`` the report does not grow with the failure
    count: at most
    :data:`~application_sdk.constants.ASSET_VALIDATION_MAX_ITEMS_PER_AXIS` failures
    and as many orphans are kept as objects, which is every row any consumer lists
    (the event matrix and both ``format_report`` bodies stop there). The counts, the
    per-rule summary and the shared report's per-kind counts still cover the whole
    batch. That is what keeps the report the scan's child process pickles back to
    the activity the same size whether one record failed or every record did.

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
        creation_rules=creation_rules,
        max_samples=max_samples,
        budget=budget if budget is not None else ScanBudget.from_deployment(),
    )

    if assets.total == 0:
        return AssetArtifactReport(
            verdict=OUTCOME_ABSENT,
            reason=f"no ndjson records found at {path}",
            assets=assets,
        )

    if assets.truncated and assets.ok:
        # Nothing wrong in the part read is not a clean artifact: the rest was never
        # checked. ``absent`` + broken is what a scan killed at the timeout reports,
        # so a generic consumer counts a stopped scan the same way, never as clean.
        # The asset event reads ``assets`` and is unaffected.
        return AssetArtifactReport(
            verdict=OUTCOME_ABSENT,
            validator_broken=True,
            reason=_truncation_reason(assets),
            assets=assets,
        )

    # The samples keep the shared list's order — every kept failure, then every
    # kept orphan — so its first ``max_items`` rows are the same rows the
    # unbounded list would have started with.
    return AssetArtifactReport(
        total=assets.total,
        passed=assets.passed,
        failures=(
            [_as_record_failure(f) for f in assets.failures]
            + [_as_reference_failure(o) for o in assets.orphans]
        ),
        failure_counts=(
            {
                "invalid": assets.failed - assets.undeserializable,
                "undecodable": assets.undeserializable,
                "missing": assets.orphaned,
            }
            if assets.failures_omitted or assets.orphans_omitted
            else None
        ),
        reason=_truncation_reason(assets),
        assets=assets,
    )


def _truncation_reason(assets: AssetValidationReport) -> str:
    """The shared report's ``reason`` for a scan that stopped at a budget."""
    if not assets.truncated:
        return ""
    label = _TRUNCATED_LABELS.get(assets.truncated, assets.truncated)
    return (
        f"scan stopped at its {label} after {assets.total} records; the counts "
        f"cover those records only"
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
#
# FND-3495 added one attribute, ``asset_validation_summary`` — additive only; every
# pre-existing key and value is untouched.

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


ASSET_VALIDATION_SUMMARY_MAX_ROWS: Final = 100
"""Row cap for the ``asset_validation_summary`` attribute. Rows are distinct
``(kind, type_name, detail)`` keys, which the schema bounds (types x checks), so a
real batch sits far below this; the cap only guards a pathological one. Dropped
rows fold into one trailing ``kind="truncated"`` row so the counts still add up."""

# pyatlan_v9's generated ``.validate()`` collects every message and raises one
# ``ValueError(f"{Type} validation failed: {errors}")`` — so ``str(exc)`` is the
# repr of a list. Every generated validator uses exactly four message shapes:
# "<field> is required", "<field> is required for creation",
# "one of <a>, <b> is required for creation" and
# "<field> '<value>' does not match expected pattern: <regex>". The rule key keeps
# only the check and the field name, never ``<value>``, so the summary carries no
# record data and stays low-cardinality.
_VALIDATE_BUNDLE = re.compile(r"^\w+ validation failed: (\[.*\])\Z", re.DOTALL)
_REQUIRED = re.compile(r"^(\w+) is required( for creation)?\Z")
_ONE_OF_REQUIRED = re.compile(r"^one of ((?:\w+, )*\w+) is required( for creation)?\Z")
_PATTERN_MISMATCH = re.compile(
    r"^(\w+) '.*' does not match expected pattern: ", re.DOTALL
)
_CONNECTION_PREFIX = re.compile(
    r"^(\w+)(?: reference)? '.*' lacks its connection prefix ", re.DOTALL
)
"""The SDK's own connection-prefix messages (see :func:`_connection_prefix_errors`)."""
_RULE_OTHER: Final = "other"


def _validation_rule(message: str) -> str:
    """Map one ``.validate()`` message to a stable, value-free rule key."""
    if match := _REQUIRED.match(message):
        check = "required_for_creation" if match.group(2) else "required"
        return f"{check}:{match.group(1)}"
    if match := _ONE_OF_REQUIRED.match(message):
        check = "one_of_required_for_creation" if match.group(2) else "one_of_required"
        return f"{check}:{match.group(1).replace(', ', '|')}"
    if match := _PATTERN_MISMATCH.match(message):
        return f"pattern:{match.group(1)}"
    if match := _CONNECTION_PREFIX.match(message):
        return f"connection_prefix:{match.group(1)}"
    return _RULE_OTHER


@functools.lru_cache(maxsize=4096)
def _validation_rules(error: str) -> tuple[str, ...]:
    """Split one ``AssetValidationFailure.errors`` entry into its rule keys.

    Cached: bundles of "is required" messages carry no record values, so the same
    string repeats for every asset of a type that misses the same fields. The list
    is read back with ``ast.literal_eval`` — a literal parser, not evaluation — and
    anything that is not the expected bundle is classified as one message.
    """
    messages: list[object] = [error]
    if match := _VALIDATE_BUNDLE.match(error):
        try:
            parsed = ast.literal_eval(match.group(1))
        except (  # conformance: ignore[E009] per-record fallback; surfaces as the "other" summary row, and a log here would fire once per failing asset
            ValueError,
            SyntaxError,
            MemoryError,
            RecursionError,
        ):
            parsed = None
        if isinstance(parsed, list):
            messages = parsed
    rules = (
        _validation_rule(message) if isinstance(message, str) else _RULE_OTHER
        for message in messages
    )
    return tuple(dict.fromkeys(rules))


def _tally_failure(
    counts: dict[_SummaryKey, int], failure: AssetValidationFailure
) -> None:
    """Add one per-asset failure to the summary counts.

    The walk calls this as it goes and :func:`asset_validation_summary_json` calls
    it over a hand-built report's list — one definition, so the two cannot count a
    failure differently.
    """
    if failure.deserialize_error:
        reason = failure.decode_reason or "unknown"
        key = ("undeserializable", failure.type_name, f"decode:{reason}")
        counts[key] = counts.get(key, 0) + 1
        return
    rules = {rule for error in failure.errors for rule in _validation_rules(error)}
    for rule in rules or (_RULE_OTHER,):
        key = ("invalid", failure.type_name, rule)
        counts[key] = counts.get(key, 0) + 1


def _tally_orphan(
    counts: dict[_SummaryKey, int],
    references: dict[_SummaryKey, int],
    orphan: ReferentialFailure,
) -> None:
    """Add one orphan, and the references to it, to the summary counts."""
    key = ("orphan", orphan.missing_type_name, orphan.relationship)
    counts[key] = counts.get(key, 0) + 1
    references[key] = references.get(key, 0) + orphan.reference_count


def asset_validation_summary_json(
    report: AssetValidationReport,
    *,
    max_rows: int = ASSET_VALIDATION_SUMMARY_MAX_ROWS,
) -> str:
    """Complete per-(kind, type, detail) failure counts for the outcome event.

    The matrix is a bounded sample of individual failures; this is the aggregate
    over **every** failure in the batch, so a fleet query can answer "which asset
    types fail which checks, how often" without the sample cap. One JSON array, each
    row ``{"kind", "type_name", "detail", "count"}``:

    * ``kind="invalid"`` — ``detail`` is a rule key such as
      ``required_for_creation:schema_qualified_name`` or ``pattern:qualified_name``
      (``other`` for a message outside pyatlan_v9's known shapes); ``count`` is the
      number of assets that broke that rule. An asset breaking several rules counts
      once in each, so these counts can sum past ``assets_invalid``.
    * ``kind="undeserializable"`` — ``detail`` is ``decode:<reason>`` (see
      :attr:`AssetValidationFailure.decode_reason`); ``count`` is records.
    * ``kind="orphan"`` — ``type_name`` is the *missing* target's type and
      ``detail`` the relationship it was referenced through; ``count`` is distinct
      missing targets and ``references`` the references to them.

    Rows are ordered by ``count`` descending (ties by key, so output is stable).
    Past ``max_rows`` the rest fold into one ``kind="truncated"`` row carrying their
    summed ``count`` and the summed ``references`` of any orphan groups among them,
    so both totals survive the cap. Like the matrix, this is not internally guarded; the emitting
    hook's ``try/except`` owns that.
    """
    if report.summary_counts is not None:
        counts = report.summary_counts
        references = report.summary_references or {}
    else:
        counts = {}
        references = {}
        for failure in report.failures:
            _tally_failure(counts, failure)
        for orphan in report.orphans:
            _tally_orphan(counts, references, orphan)

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    rows: list[dict[str, object]] = []
    for (kind, type_name, detail), count in ordered[:max_rows]:
        row: dict[str, object] = {
            "kind": kind,
            "type_name": type_name,
            "detail": detail,
            "count": count,
        }
        if kind == "orphan":
            row["references"] = references.get((kind, type_name, detail), 0)
        rows.append(row)
    if dropped := ordered[max_rows:]:
        rows.append(
            {
                "kind": "truncated",
                "type_name": "",
                "detail": "",
                "count": sum(count for _, count in dropped),
                # Orphan groups past the cap still carry references; without this
                # the batch's reference total would be under-reported.
                "references": sum(
                    references.get(key, 0) for key, _ in dropped if key[0] == "orphan"
                ),
            }
        )
    return orjson.dumps(rows).decode()


@dataclass(frozen=True)
class AssetBatchScope:
    """How one validated hand-off was assembled — context the report cannot know.

    Without it an orphan count cannot be read: the same number means something
    different for one ``upload`` of a single file, and for an ``upload_refs`` fan-in
    whose parts were validated together.
    """

    upload_kind: typing.Literal["upload", "upload_refs"]
    """Which framework task delivered the batch."""
    parts_validated: int
    """Local paths validated together as one batch."""
    parts_not_local: int
    """Declared transformed parts absent from this pod (streamed from the store),
    which turn the orphan pass off — see ``assets_referential_check``."""


def asset_validation_event_fields(
    report: AssetValidationReport,
    *,
    app_name: str,
    max_items: int = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
    scope: AssetBatchScope | None = None,
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
        scope: How the batch was assembled. The upload hook always passes it;
            without it the three ``assets_upload_kind`` / ``assets_parts_*`` keys
            are omitted rather than guessed.

    Returns:
        Keyword arguments for the ``ASSET_VALIDATION_EVENT`` log call.
    """
    return {
        "outcome": "clean" if report.ok else "flagged",
        "app_name": app_name,
        "assets_total": report.total,
        "assets_passed": report.passed,
        "assets_invalid": report.failed - report.undeserializable,
        "assets_orphaned": report.orphaned,
        "assets_undeserializable": report.undeserializable,
        ASSET_VALIDATION_MATRIX_KEY: asset_validation_matrix_json(
            report, max_items=max_items
        ),
        ASSET_VALIDATION_SUMMARY_KEY: asset_validation_summary_json(report),
        "assets_referential_check": report.referential_check,
        **(
            {}
            if scope is None
            else {
                "assets_upload_kind": scope.upload_kind,
                "assets_parts_validated": scope.parts_validated,
                "assets_parts_not_local": scope.parts_not_local,
            }
        ),
        # Present only on a scan that stopped at a budget, so the row of every
        # complete scan is exactly what it was before the budgets existed.
        **(
            {ASSET_VALIDATION_TRUNCATED_KEY: report.truncated}
            if report.truncated
            else {}
        ),
    }

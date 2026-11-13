"""Lineage coverage taxonomy: closed SDK enums, reason leaves, app subclasses.

Modelled on the AppErrors hierarchy (ADR-0013). The SDK owns the closed sets:

* :class:`ReasonCategory`: why lineage is missing, low cardinality. The only
  reason-derived value allowed as a metric label.
* :class:`GapCategory`: which stage owns the gap. The source, the app's own
  processing, or the asset lookup that resolves refs after the app.
* :class:`Verdict`: the per-asset answer.

A reason is a class, not a string. The SDK ships one leaf per
:class:`ReasonCategory` plus a handful of generic, connector-agnostic reasons.
An app subclasses the closest one and gives it its own ``code``::

    class DatasourceHasNoUpstreamTables(NoUpstreamTables):
        code = "DATASOURCE_HAS_NO_UPSTREAM_TABLES"
        description = "The Metadata API returned no upstream tables"
        customer_message = "Check the crawler user can see the connections."

``category``, ``gap`` and ``expected`` are inherited unless the subclass
overrides them, the same way an AppError leaf subclass inherits ``category``
and may pin ``audience``. Apps cannot add a category or a gap: those are SDK
changes, so coverage aggregates across the fleet.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import ClassVar

from application_sdk._logging import get_logger

_logger = get_logger(__name__)

_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ReasonCategory(StrEnum):
    """Why lineage is missing. Frozen at twelve, as in application-sdk#2204.

    Each member is a metric-label value, so growing the set is a reviewed SDK
    change. ``CACHE_MISS`` predates the asset lookup: under ARS it means the
    lookup found no (or no single) match for a ref.
    """

    PARSER_FAILURE = "PARSER_FAILURE"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    CACHE_MISS = "CACHE_MISS"
    UNSUPPORTED_SOURCE_FEATURE = "UNSUPPORTED_SOURCE_FEATURE"
    MISSING_METADATA = "MISSING_METADATA"
    NO_UPSTREAM_DATA = "NO_UPSTREAM_DATA"
    NO_SEARCHABLE_DIALECT = "NO_SEARCHABLE_DIALECT"
    PROCESS_MAPPING_FAILURE = "PROCESS_MAPPING_FAILURE"
    CONNECTOR_LIMITATION = "CONNECTOR_LIMITATION"
    RELATIONSHIP_FALLBACK = "RELATIONSHIP_FALLBACK"
    OBSERVABILITY_DISABLED = "OBSERVABILITY_DISABLED"
    UNINSTRUMENTED_PATH = "UNINSTRUMENTED_PATH"


class GapCategory(StrEnum):
    """Which stage owns a gap."""

    SOURCE = "SOURCE"
    """The source system returned too little to build the edge."""
    PROCESSING = "PROCESSING"
    """The app could not turn what it got into lineage (parse, dialect, mapping)."""
    ASSET_RESOLUTION = "ASSET_RESOLUTION"
    """A ref the app emitted could not be matched to an asset in Atlan."""


class Verdict(StrEnum):
    """The per-asset answer."""

    VALID = "VALID"
    INVALID = "INVALID"
    """Lineage exists but is incomplete, or may point at the wrong asset."""
    MISSING = "MISSING"
    VALID_NONE = "VALID_NONE"
    """No lineage, and none is expected (a file source, a customer setting)."""
    PENDING = "PENDING"
    """Refs were handed to the asset lookup and its outcome is not known yet."""


class LineageReason:
    """Base for every lineage reason. Record a leaf or a subclass, never this.

    Reasons are classes and are never instantiated: everything about a reason
    is fixed per class (a ClassVar), as ADR-0013 does for error codes. Every
    class declares its own ``code``. Two classes share a code only when one
    restates the other: an app subclassing an SDK reason under the same code to
    change its wording (see :class:`ReasonRegistry`).

    Attributes:
        code: Unique, UPPER_SNAKE. Recorded per asset; never a metric label.
        category: The closed :class:`ReasonCategory` it aggregates to.
        gap: The stage that owns the gap, or ``None`` when it has no owner
            (an uninstrumented path, observability switched off).
        expected: ``True`` when no lineage is the correct outcome. The asset
            is then :attr:`Verdict.VALID_NONE`, not :attr:`Verdict.MISSING`.
        description: One line for support.
        customer_message: What a customer can be told, or ``None``.
    """

    code: ClassVar[str]
    category: ClassVar[ReasonCategory]
    gap: ClassVar[GapCategory | None]
    expected: ClassVar[bool] = False
    description: ClassVar[str] = ""
    customer_message: ClassVar[str | None] = None

    def __new__(cls, *args: object, **kwargs: object) -> LineageReason:
        raise TypeError(
            f"{cls.__name__} is a reason class; record the class itself, "
            "do not instantiate it"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        code = cls.__dict__.get("code")
        if not isinstance(code, str) or not _CODE_PATTERN.match(code):
            raise TypeError(
                f"{cls.__name__} must declare its own UPPER_SNAKE `code`; got {code!r}"
            )
        if not isinstance(getattr(cls, "category", None), ReasonCategory):
            raise TypeError(f"{cls.__name__}.category must be a ReasonCategory")
        gap = getattr(cls, "gap", None)
        if gap is not None and not isinstance(gap, GapCategory):
            raise TypeError(f"{cls.__name__}.gap must be a GapCategory or None")

    @classmethod
    def verdict(cls) -> Verdict:
        """The verdict an asset with no lineage gets for this reason."""
        return Verdict.VALID_NONE if cls.expected else Verdict.MISSING


# ── One leaf per ReasonCategory ─────────────────────────────────────────────


class ParserFailure(LineageReason):
    code = "PARSER_FAILURE"
    category = ReasonCategory.PARSER_FAILURE
    gap = GapCategory.PROCESSING
    description = "The app could not parse the source's definition"


class AssetNotFound(LineageReason):
    code = "ASSET_NOT_FOUND"
    category = ReasonCategory.ASSET_NOT_FOUND
    gap = GapCategory.SOURCE
    description = "An asset the source referenced is not in this extract"


class CacheMiss(LineageReason):
    code = "CACHE_MISS"
    category = ReasonCategory.CACHE_MISS
    gap = GapCategory.ASSET_RESOLUTION
    description = "The asset lookup found no single match for a ref"


class UnsupportedSourceFeature(LineageReason):
    code = "UNSUPPORTED_SOURCE_FEATURE"
    category = ReasonCategory.UNSUPPORTED_SOURCE_FEATURE
    gap = GapCategory.PROCESSING
    description = "The source uses a feature the app does not support"


class MissingMetadata(LineageReason):
    code = "MISSING_METADATA"
    category = ReasonCategory.MISSING_METADATA
    gap = GapCategory.SOURCE
    description = "The source omitted a field the edge needs"


class NoUpstreamData(LineageReason):
    code = "NO_UPSTREAM_DATA"
    category = ReasonCategory.NO_UPSTREAM_DATA
    gap = GapCategory.SOURCE
    description = "The source returned no upstream for this asset"


class NoSearchableDialect(LineageReason):
    code = "NO_SEARCHABLE_DIALECT"
    category = ReasonCategory.NO_SEARCHABLE_DIALECT
    gap = GapCategory.PROCESSING
    description = "No upstream has a connection type to resolve against"


class ProcessMappingFailure(LineageReason):
    code = "PROCESS_MAPPING_FAILURE"
    category = ReasonCategory.PROCESS_MAPPING_FAILURE
    gap = GapCategory.PROCESSING
    description = "The app could not map the asset to its Process"


class ConnectorLimitation(LineageReason):
    code = "CONNECTOR_LIMITATION"
    category = ReasonCategory.CONNECTOR_LIMITATION
    gap = GapCategory.PROCESSING
    description = "The connector does not build this lineage by design"


class RelationshipFallback(LineageReason):
    code = "RELATIONSHIP_FALLBACK"
    category = ReasonCategory.RELATIONSHIP_FALLBACK
    gap = GapCategory.PROCESSING
    description = "Lineage fell back to a relationship instead of a Process"


class ObservabilityDisabled(LineageReason):
    code = "OBSERVABILITY_DISABLED"
    category = ReasonCategory.OBSERVABILITY_DISABLED
    gap = None
    description = "Coverage tracking was off for this run"


class UninstrumentedPath(LineageReason):
    code = "UNINSTRUMENTED_PATH"
    category = ReasonCategory.UNINSTRUMENTED_PATH
    gap = None
    description = "Registered, but no code path recorded lineage or a reason"


# ── Generic reasons the SDK ships (connector-agnostic) ──────────────────────


class CustomSqlParserFailure(ParserFailure):
    code = "CUSTOM_SQL_PARSER_FAILURE"
    description = "Parsing the custom SQL threw an exception"
    customer_message = "Atlan could not parse this asset's custom SQL."


class CustomSqlTableUnparsed(ParserFailure):
    code = "CUSTOM_SQL_TABLE_UNPARSED"
    description = "A table the SQL parser returned had no database, schema or name"


class CustomSqlNoTablesParsed(ParserFailure):
    code = "CUSTOM_SQL_NO_TABLES_PARSED"
    description = "The custom SQL parsed but yielded no usable tables"


class CustomSqlEmptyQuery(MissingMetadata):
    code = "CUSTOM_SQL_EMPTY_QUERY"
    description = "The source returned the custom SQL empty"
    customer_message = "The source returned this asset's custom SQL empty."


class UpstreamTableNameMissing(MissingMetadata):
    code = "UPSTREAM_TABLE_NAME_MISSING"
    description = "The source returned an upstream table without its name"
    customer_message = (
        "The source returned the upstream tables without names, so they could "
        "not be linked."
    )


class UpstreamIdentityTooSparse(MissingMetadata):
    code = "UPSTREAM_IDENTITY_TOO_SPARSE"
    description = "Too few of database, schema and table to emit a resolvable ref"


class NoUpstreamTables(NoUpstreamData):
    code = "NO_UPSTREAM_TABLES"
    description = "The source returned no upstream tables"


class ColumnProcessNullQualifiedName(MissingMetadata):
    code = "COLUMN_PROCESS_NULL_QUALIFIED_NAME"
    # A null qualifiedName on an emitted ColumnProcess is an app bug.
    gap = GapCategory.PROCESSING
    description = "A column edge had a null qualifiedName, so it was dropped"


class NonSqlSource(ConnectorLimitation):
    code = "NON_SQL_SOURCE"
    expected = True
    description = "Every upstream is a file or a source with no SQL connector"
    customer_message = (
        "This asset reads only files or sources Atlan has no SQL connector "
        "for, so there is no upstream table to link to."
    )


class GenericConnectionType(UnsupportedSourceFeature):
    code = "GENERIC_CONNECTION_TYPE"
    description = "The upstream is a generic ODBC/JDBC connection; dialect unknown"


class UpstreamNotInAtlan(CacheMiss):
    code = "UPSTREAM_NOT_IN_ATLAN"
    description = "The asset lookup matched no asset for the ref"
    customer_message = (
        "The upstream table is not in Atlan. Crawl the source (or the missing "
        "database or project) to link it."
    )


class UpstreamAmbiguous(CacheMiss):
    code = "UPSTREAM_AMBIGUOUS"
    description = "The asset lookup matched more than one asset and picked one"


SDK_REASONS: tuple[type[LineageReason], ...] = (
    ParserFailure,
    AssetNotFound,
    CacheMiss,
    UnsupportedSourceFeature,
    MissingMetadata,
    NoUpstreamData,
    NoSearchableDialect,
    ProcessMappingFailure,
    ConnectorLimitation,
    RelationshipFallback,
    ObservabilityDisabled,
    UninstrumentedPath,
    CustomSqlParserFailure,
    CustomSqlTableUnparsed,
    CustomSqlNoTablesParsed,
    CustomSqlEmptyQuery,
    UpstreamTableNameMissing,
    UpstreamIdentityTooSparse,
    NoUpstreamTables,
    ColumnProcessNullQualifiedName,
    NonSqlSource,
    GenericConnectionType,
    UpstreamNotInAtlan,
    UpstreamAmbiguous,
)
"""Every reason the SDK ships, leaves first."""


class ReasonRegistry:
    """Code -> reason class for one app: the SDK's reasons plus the app's own.

    Only needed where a code arrives as a string (a row read back, a call site
    not yet migrated to classes). Unknown codes resolve to
    :class:`UninstrumentedPath` with one warning per code, so a typo shows up
    as uninstrumented rather than silently counting as covered.
    """

    def __init__(self, reasons: Iterable[type[LineageReason]] = ()) -> None:
        self._by_code: dict[str, type[LineageReason]] = {}
        self._warned: set[str] = set()
        for reason in (*SDK_REASONS, *reasons):
            if not (isinstance(reason, type) and issubclass(reason, LineageReason)):
                raise TypeError(f"{reason!r} is not a LineageReason subclass")
            existing = self._by_code.get(reason.code)
            if existing is not None and existing is not reason:
                # An app may restate a reason it inherits (its own customer
                # wording, say) by subclassing it under the same code. The
                # category must stay, so counts by category don't move.
                if not (
                    issubclass(reason, existing)
                    and reason.category is existing.category
                ):
                    raise ValueError(
                        f"Reason code {reason.code!r} is declared by both "
                        f"{existing.__name__} and {reason.__name__}"
                    )
            self._by_code[reason.code] = reason

    def get(self, code: str) -> type[LineageReason] | None:
        return self._by_code.get(code)

    def resolve(self, code: str | None) -> type[LineageReason]:
        if code is None:
            return UninstrumentedPath
        reason = self._by_code.get(code)
        if reason is not None:
            return reason
        if code not in self._warned:
            self._warned.add(code)
            _logger.warning(
                "Unregistered lineage reason code %s; counting it as %s",
                code,
                UninstrumentedPath.code,
            )
        return UninstrumentedPath

    def __contains__(self, code: object) -> bool:
        return code in self._by_code

    def __iter__(self) -> Iterator[type[LineageReason]]:
        return iter(self._by_code.values())

    def __len__(self) -> int:
        return len(self._by_code)

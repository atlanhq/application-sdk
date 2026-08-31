"""One shared golden-diff assertion for connector output.

Compares metadata a connector just produced against a static golden fixture
captured from the same environment, and raises :class:`AssertionError` with a
readable field-level report when the diff is not acceptable.

This is deliberately a different comparison from the two that already exist:

* :mod:`application_sdk.testing.parity.comparator` compares two *extraction
  runs* (baseline vs candidate) discovered from two directory trees, and is
  CLI-shaped.
* :mod:`application_sdk.testing.integration.comparison` compares against a
  baseline captured on *other* infrastructure, so it ignores every
  environment-scoped field including ``qualifiedName``.

Here the golden fixture comes from the same environment and is re-baselined in
place, so ``qualifiedName`` is the comparison *key* and environment-scoped
fields are kept — a differing ``qualifiedName`` is a real regression in
qualified-name construction, not noise. Only
:data:`~application_sdk.testing.volatile_fields.RUN_VOLATILE_FIELDS` is
stripped by default.

What "order-independent" covers
-------------------------------

The diff is keyed, so it is order-independent *across records*: produced and
golden files may hold the same records in any order. *Within* a record it is
order-sensitive: nested dicts are recursed, but a list field compares
positionally, so a reordered ``attributes.columns`` reports as a field
mismatch. When a relationship array has no guaranteed ordering, sort it before
capture and before comparison (or ignore that one field) — do not downgrade
the whole typename to ``NO_EXTRAS`` for ordering noise, because that silences
every other mismatch on the typename with it.

Grouping: what a "typename" is here
-----------------------------------

Records are bucketed by ``record["typeName"]`` by default, which fits Atlas
transformed records. Raw/extract-tier records usually have no ``typeName``, so
every one of them lands in a single ``"<no typeName>"`` bucket (a sentinel no
real typename can collide with) and ``rules=`` becomes inert — there is no
typename for a rule to key on. Two legitimate patterns for that tier:

* pass ``group_by=`` with the record shape's own discriminator, which restores
  per-group rules::

      assert_matches_golden(
          produced,
          golden,
          key=lambda r: r["id"],
          group_by=lambda r: r["record_type"],
          rules=RULES,
      )

* or loop in the test, one call per typename, with ``default_rule=`` carrying
  that typename's strictness. This is the right shape when the produced and
  golden records for each typename are loaded from separate files anyway::

      for typename, records in produced_by_typename.items():
          assert_matches_golden(
              records,
              golden_by_typename[typename],
              key=raw_key,
              default_rule=RULES[typename],
          )

Per-typename strictness
-----------------------

A golden corpus captured from a live source is rarely a matched
extract-then-transform pair. Truncating a capture at any single typename
changes what *other* typenames can legitimately reference, so a uniform
byte-for-byte gate across every typename produces failures that are artifacts
of the capture rather than bugs in the connector.

Rather than leaving that reasoning in prose, each typename carries a
:class:`TypenameRule` — reviewable config, so a reviewer can see at a glance
which typenames are not actually gated::

    RULES = {
        "projects": TypenameRule(),
        "reports": TypenameRule(ignore_fields=frozenset({"source_url"})),
        "cubes": TypenameRule(policy=DiffPolicy.NO_EXTRAS),
        "columns": TypenameRule(policy=DiffPolicy.INFO_ONLY),
    }

    assert_matches_golden(produced, golden, rules=RULES)

Strictness and field exclusions are separate dials on purpose. "Compare this
typename byte-for-byte except for one field that structurally cannot match" is
not a weaker gate — it is a full-strength gate with a documented exclusion, and
collapsing the two into a single enum would make it unexpressible. A legitimate
example: a client that computes ``source_url`` from its own configured host
rather than from any source response field can never match a sanitized fixture
on that field, while every other field on that typename still must match.
"""

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.integration._errors import (
    GoldenDuplicateKeyError,
    GoldenRuleError,
)
from application_sdk.testing.parity.comparator import (
    diff_dicts,
    get_qualified_name,
    strip_volatile,
)
from application_sdk.testing.volatile_fields import RUN_VOLATILE_FIELDS

logger = get_logger(__name__)

_MAX_KEYS_SHOWN = 10
_MAX_MISMATCHES_SHOWN = 5
_MAX_FIELDS_SHOWN = 10
_MAX_VALUE_CHARS = 200

NO_TYPENAME = "<no typeName>"
"""Fallback bucket for records without a ``typeName``.

A sentinel rather than ``"Unknown"`` because a record whose ``typeName``
genuinely is ``"Unknown"`` must not share a bucket — and a ``rules`` entry —
with the untyped records.
"""


class DiffPolicy(StrEnum):
    """How strictly a typename's diff is gated."""

    STRICT = "STRICT"
    """Extra and mismatched assets both fail the assertion."""

    NO_EXTRAS = "NO_EXTRAS"
    """Only extra assets fail.

    For typenames whose relationship arrays are derived from other typenames
    that are themselves truncated in the capture, so legitimately shorter
    arrays are an artifact. Still catches qualified-name construction bugs and
    wrong-parent bugs, which is most of the value.
    """

    INFO_ONLY = "INFO_ONLY"
    """Nothing fails. The diff is computed and reported, not asserted.

    For typenames whose own universe in the capture is demonstrably unmatched.
    Records the gap honestly instead of pretending it is gated.
    """


class DuplicateKeyPolicy(StrEnum):
    """What a non-unique join key on either side does to the comparison."""

    ERROR = "error"
    """Raise :class:`GoldenDuplicateKeyError` — the default.

    Two records sharing a join key silently shadow each other, so the
    comparison is not the one the caller thinks it is.
    """

    LAST_WINS = "last-wins"
    """Keep the last record per key and surface the collisions on the report.

    The escape hatch, named as an enum so its uses stay greppable across
    consumer suites.
    """


@dataclass(frozen=True)
class TypenameRule:
    """The gating decision for one typename.

    Attributes:
        policy: Which diff categories fail the assertion.
        ignore_fields: Fields excluded from comparison for this typename only,
            on top of the global ignore set. For fields that structurally
            cannot match a golden fixture.
        tolerate_missing: When True, assets present in golden but absent from
            produced output do not fail. Set this when the golden capture is
            known to be a superset of what the produced run covers. Only
            meaningful with ``policy=STRICT`` — the weaker policies never gate
            on missing assets, so combining them with this flag raises
            :class:`GoldenRuleError` rather than reading as stricter than it is.
    """

    policy: DiffPolicy = DiffPolicy.STRICT
    ignore_fields: frozenset[str] = frozenset()
    tolerate_missing: bool = False

    def __post_init__(self) -> None:
        if self.tolerate_missing and self.policy is not DiffPolicy.STRICT:
            raise GoldenRuleError(
                message=(
                    f"TypenameRule(policy={self.policy}, tolerate_missing=True) "
                    "is a no-op: only STRICT gates on missing assets, so this "
                    "rule is not stricter than the policy alone. Drop "
                    "tolerate_missing, or use policy=STRICT."
                ),
                field="tolerate_missing",
                constraint="strict_policy_only",
            )


DEFAULT_RULE = TypenameRule()


@dataclass(frozen=True)
class FieldDiff:
    """One field whose value differs between the golden fixture and ours."""

    field_path: str
    golden_value: Any
    ours_value: Any


@dataclass(frozen=True)
class AssetMismatch:
    """An asset present on both sides whose fields differ."""

    key: str
    field_diffs: tuple[FieldDiff, ...]


@dataclass(frozen=True)
class TypenameDiff:
    """The diff for a single typename, and whether it gates.

    ``duplicate_keys_ours`` / ``duplicate_keys_golden`` are only ever populated
    under ``on_duplicate_key="last-wins"``; the default raises instead.
    """

    typename: str
    rule: TypenameRule
    produced_count: int
    golden_count: int
    missing_in_ours: tuple[str, ...] = ()
    extra_in_ours: tuple[str, ...] = ()
    mismatches: tuple[AssetMismatch, ...] = ()
    duplicate_keys_ours: tuple[str, ...] = ()
    duplicate_keys_golden: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[str, ...]:
        """Reasons this typename fails, empty when it passes."""
        if self.rule.policy is DiffPolicy.INFO_ONLY:
            return ()

        reasons: list[str] = []
        if self.extra_in_ours:
            reasons.append(f"{len(self.extra_in_ours)} extra in ours")

        if self.rule.policy is DiffPolicy.STRICT:
            if self.mismatches:
                reasons.append(f"{len(self.mismatches)} mismatched")
            if self.missing_in_ours and not self.rule.tolerate_missing:
                reasons.append(f"{len(self.missing_in_ours)} missing from ours")

        return tuple(reasons)

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)

    @property
    def has_diffs(self) -> bool:
        """True when anything differs, gated or not."""
        return bool(self.missing_in_ours or self.extra_in_ours or self.mismatches)


@dataclass(frozen=True)
class GoldenReport:
    """The full golden comparison across every typename.

    Attributes:
        diffs: The diff for every typename seen on either side.
        produced_skipped: Produced records dropped before comparison because
            ``key(record)`` was empty.
        golden_skipped: Golden records dropped for the same reason. A non-zero
            count is not automatically an error — a corpus can legitimately
            carry keyless noise rows — but it is the first thing to check when
            the compared counts look too low.
    """

    diffs: tuple[TypenameDiff, ...] = ()
    produced_skipped: int = 0
    golden_skipped: int = 0

    @property
    def failing(self) -> tuple[TypenameDiff, ...]:
        return tuple(d for d in self.diffs if d.has_failures)

    @property
    def has_failures(self) -> bool:
        return bool(self.failing)

    def format_report(self) -> str:
        """Format the report for pytest output."""
        if not self.diffs:
            return "\n".join(["No typenames compared.", *self._skipped_lines()])

        lines: list[str] = []
        if self.has_failures:
            gating = ", ".join(d.typename for d in self.failing)
            lines.append(f"Golden comparison FAILED for: {gating}")
        else:
            lines.append("Golden comparison passed.")
        lines.append("")

        lines.append("Summary:")
        for diff in self.diffs:
            verdict = "FAIL" if diff.has_failures else "ok"
            if diff.rule.policy is DiffPolicy.INFO_ONLY:
                verdict = "info"
            lines.append(
                f"  [{verdict}] {diff.typename} ({diff.rule.policy}): "
                f"ours={diff.produced_count} golden={diff.golden_count} "
                f"missing={len(diff.missing_in_ours)} "
                f"extra={len(diff.extra_in_ours)} "
                f"mismatched={len(diff.mismatches)}"
            )
        lines.extend(self._skipped_lines())
        lines.extend(self._duplicate_lines())
        lines.append("")

        for diff in self.diffs:
            if not diff.has_diffs:
                continue
            lines.extend(_format_typename_detail(diff))

        return "\n".join(lines)

    def _skipped_lines(self) -> list[str]:
        if not (self.produced_skipped or self.golden_skipped):
            return []
        return [
            (
                f"  skipped for empty key: ours={self.produced_skipped} "
                f"golden={self.golden_skipped}"
            )
        ]

    def _duplicate_lines(self) -> list[str]:
        lines: list[str] = []
        for diff in self.diffs:
            for label, keys in (
                ("ours", diff.duplicate_keys_ours),
                ("golden", diff.duplicate_keys_golden),
            ):
                if not keys:
                    continue
                shown = ", ".join(keys[:_MAX_KEYS_SHOWN])
                lines.append(
                    f"  duplicate keys in {label} for {diff.typename} "
                    f"({len(keys)}): {shown}"
                )
        return lines


def diff_golden(
    produced: Iterable[Mapping[str, Any]],
    golden: Iterable[Mapping[str, Any]],
    *,
    key: Callable[[Mapping[str, Any]], str] = get_qualified_name,
    rules: Mapping[str, TypenameRule] | None = None,
    default_rule: TypenameRule = DEFAULT_RULE,
    ignore: Collection[str] = RUN_VOLATILE_FIELDS,
    extra_ignore: Collection[str] = (),
    on_duplicate_key: DuplicateKeyPolicy | Literal["error", "last-wins"] = (
        DuplicateKeyPolicy.ERROR
    ),
    group_by: Callable[[Mapping[str, Any]], str] | None = None,
    expect_typenames: Collection[str] | None = None,
) -> GoldenReport:
    """Compare produced records against a golden fixture without asserting.

    "Without asserting" means the *diff* is never asserted: a report is
    returned however badly the two sides differ. Malformed input still raises —
    a non-unique join key (``DuplicateKeyPolicy.ERROR``) and an unmet
    ``expect_typenames`` floor are both wrong-test conditions, not diffs.

    The diff is keyed and order-independent across records, and order-sensitive
    within a field — see the module docstring before reaching for a weaker
    ``DiffPolicy`` over list-ordering noise.

    Args:
        produced: Records the connector just produced.
        golden: Records loaded from the golden fixture.
        key: Extracts the join key from a record. Defaults to
            ``attributes.qualifiedName``. Records with an empty key are skipped
            and counted in ``produced_skipped`` / ``golden_skipped``.
        rules: Per-typename gating rules. Typenames absent from this mapping
            use ``default_rule``.
        default_rule: Rule applied to typenames not named in ``rules``.
        ignore: Fields stripped at every depth before comparison. Defaults to
            run-volatile fields only — environment-scoped fields are kept
            because the golden fixture comes from the same environment. Passing
            ``ignore=`` REPLACES the default ``RUN_VOLATILE_FIELDS``; to add
            fields on top of the canonical three, use ``extra_ignore=``. The
            replace-shape is deliberate: it is the pinned contract the piloted
            consumer suites already call, and an additive ``ignore=`` would
            silently widen what those suites strip.
        extra_ignore: Fields stripped in addition to ``ignore``, so the
            canonical run-volatile set is kept.
        on_duplicate_key: :attr:`DuplicateKeyPolicy.ERROR` (default) raises
            :class:`GoldenDuplicateKeyError` when two records on the same side
            share a join key, because a non-unique key means the comparison is
            not the one the caller thinks it is.
            :attr:`DuplicateKeyPolicy.LAST_WINS` keeps the last record per key
            and surfaces the collisions on the report instead. The equivalent
            string literals are accepted.
        group_by: Buckets records for ``rules=`` lookup. ``None`` uses
            ``record["typeName"]``, falling back to :data:`NO_TYPENAME` — see
            the module docstring for the raw/extract-tier case.
        expect_typenames: Coverage floor. When given, every named typename must
            appear in the report with at least one golden record, else
            ``AssertionError``.

    Returns:
        GoldenReport: The diff for every typename seen on either side.

    Raises:
        GoldenDuplicateKeyError: If ``on_duplicate_key`` is
            :attr:`DuplicateKeyPolicy.ERROR` and either side has two records
            sharing a join key.
        AssertionError: If ``expect_typenames`` names a typename with no golden
            records.
    """
    rules = rules or {}
    duplicate_key_policy = DuplicateKeyPolicy(on_duplicate_key)
    produced_by_type, produced_skipped, produced_dupes = _group_by_typename(
        produced, key, "produced", group_by
    )
    golden_by_type, golden_skipped, golden_dupes = _group_by_typename(
        golden, key, "golden", group_by
    )
    if duplicate_key_policy is DuplicateKeyPolicy.ERROR:
        _raise_on_duplicates(produced_dupes, golden_dupes)

    ignore_all = frozenset(ignore) | frozenset(extra_ignore)

    diffs: list[TypenameDiff] = []
    for typename in sorted(set(produced_by_type) | set(golden_by_type)):
        rule = rules.get(typename, default_rule)
        ignored = ignore_all | rule.ignore_fields
        ours = produced_by_type.get(typename, {})
        theirs = golden_by_type.get(typename, {})

        mismatches: list[AssetMismatch] = []
        for asset_key in sorted(set(ours) & set(theirs)):
            field_diffs = diff_dicts(
                strip_volatile(dict(theirs[asset_key]), ignored),
                strip_volatile(dict(ours[asset_key]), ignored),
            )
            if field_diffs:
                mismatches.append(
                    AssetMismatch(
                        key=asset_key,
                        field_diffs=tuple(
                            FieldDiff(
                                field_path=d.field_path,
                                golden_value=d.baseline_value,
                                ours_value=d.candidate_value,
                            )
                            for d in field_diffs
                        ),
                    )
                )

        diffs.append(
            TypenameDiff(
                typename=typename,
                rule=rule,
                produced_count=len(ours),
                golden_count=len(theirs),
                missing_in_ours=tuple(sorted(set(theirs) - set(ours))),
                extra_in_ours=tuple(sorted(set(ours) - set(theirs))),
                mismatches=tuple(mismatches),
                duplicate_keys_ours=tuple(sorted(produced_dupes.get(typename, ()))),
                duplicate_keys_golden=tuple(sorted(golden_dupes.get(typename, ()))),
            )
        )

    report = GoldenReport(
        diffs=tuple(diffs),
        produced_skipped=produced_skipped,
        golden_skipped=golden_skipped,
    )
    if expect_typenames is not None:
        _assert_expected_typenames(report, expect_typenames)
    return report


def assert_matches_golden(
    produced: Iterable[Mapping[str, Any]],
    golden: Iterable[Mapping[str, Any]],
    *,
    key: Callable[[Mapping[str, Any]], str] = get_qualified_name,
    rules: Mapping[str, TypenameRule] | None = None,
    default_rule: TypenameRule = DEFAULT_RULE,
    ignore: Collection[str] = RUN_VOLATILE_FIELDS,
    extra_ignore: Collection[str] = (),
    on_duplicate_key: DuplicateKeyPolicy | Literal["error", "last-wins"] = (
        DuplicateKeyPolicy.ERROR
    ),
    group_by: Callable[[Mapping[str, Any]], str] | None = None,
    expect_typenames: Collection[str] | None = None,
) -> GoldenReport:
    """Assert produced records match a golden fixture, per typename rules.

    The diff is keyed and order-independent across records, and order-sensitive
    within a field — a reordered list value reports as a mismatch (see the
    module docstring).

    Args:
        produced: Records the connector just produced.
        golden: Records loaded from the golden fixture.
        key: Extracts the join key from a record.
        rules: Per-typename gating rules.
        default_rule: Rule applied to typenames not named in ``rules``.
        ignore: Fields stripped at every depth before comparison. Passing
            ``ignore=`` REPLACES the default ``RUN_VOLATILE_FIELDS``; to add
            fields on top of the canonical three, use ``extra_ignore=``. See
            :func:`diff_golden` for why the replace-shape is deliberate.
        extra_ignore: Fields stripped in addition to ``ignore``.
        on_duplicate_key: See :func:`diff_golden`.
        group_by: See :func:`diff_golden`.
        expect_typenames: Coverage floor; every named typename must be present
            with at least one golden record.

    Returns:
        GoldenReport: The full report, also on success, so callers can inspect
        the diffs that were reported but not gated.

    Raises:
        AssertionError: If nothing was compared at all, if ``expect_typenames``
            is not met, or if any typename's rule is violated. The message is
            the formatted field-level report.
        GoldenDuplicateKeyError: If a join key is not unique — see
            ``on_duplicate_key``.
    """
    report = diff_golden(
        produced,
        golden,
        key=key,
        rules=rules,
        default_rule=default_rule,
        ignore=ignore,
        extra_ignore=extra_ignore,
        on_duplicate_key=on_duplicate_key,
        group_by=group_by,
        expect_typenames=expect_typenames,
    )
    if not report.diffs:
        raise AssertionError(
            "Golden assertion is vacuous: nothing was compared. "
            f"{report.produced_skipped} produced / {report.golden_skipped} golden "
            f"record(s) were skipped for an empty comparison key from "
            f"key={_callable_name(key)}. Zero comparisons is a broken test, "
            "never a pass."
        )
    if report.has_failures:
        raise AssertionError(report.format_report())

    logger.info("Golden comparison passed across %d typenames", len(report.diffs))
    return report


def _group_by_typename(
    records: Iterable[Mapping[str, Any]],
    key: Callable[[Mapping[str, Any]], str],
    side: str,
    group_by: Callable[[Mapping[str, Any]], str] | None = None,
) -> tuple[dict[str, dict[str, Mapping[str, Any]]], int, dict[str, list[str]]]:
    """Index records by group then by join key.

    Returns the index, the count of records dropped for an empty key, and the
    join keys that collided per group. Collisions are reported rather than
    resolved here; the caller decides whether they are fatal.
    """
    grouped: dict[str, dict[str, Mapping[str, Any]]] = {}
    duplicates: dict[str, list[str]] = {}
    skipped = 0
    for record in records:
        asset_key = key(record)
        if not asset_key:
            skipped += 1
            continue
        typename = (
            group_by(record) if group_by else (record.get("typeName") or NO_TYPENAME)
        )
        bucket = grouped.setdefault(typename, {})
        if asset_key in bucket:
            duplicates.setdefault(typename, []).append(asset_key)
        bucket[asset_key] = record

    if skipped:
        logger.warning("Skipped %d %s record(s) with no comparison key", skipped, side)
    return grouped, skipped, duplicates


def _raise_on_duplicates(
    produced_dupes: dict[str, list[str]],
    golden_dupes: dict[str, list[str]],
) -> None:
    """Reject a join key that is not unique on either side."""
    for side, dupes in (("produced", produced_dupes), ("golden", golden_dupes)):
        if not dupes:
            continue
        details = "; ".join(
            f"{typename}: {', '.join(sorted(set(keys))[:_MAX_KEYS_SHOWN])}"
            for typename, keys in sorted(dupes.items())
        )
        raise GoldenDuplicateKeyError(
            message=(
                f"Duplicate join key(s) on the {side} side — {details}. "
                "The key= callable must be unique per record, otherwise records "
                "silently shadow each other and the comparison is not the one "
                "you think it is."
            ),
            field="key",
            constraint="unique_per_record",
            value_summary=details,
            suggested_action=(
                "Fix the key= callable, or pass "
                "on_duplicate_key=DuplicateKeyPolicy.LAST_WINS to keep the old "
                "last-write-wins behaviour."
            ),
        )


def _assert_expected_typenames(
    report: GoldenReport,
    expect_typenames: Collection[str],
) -> None:
    """Enforce the caller's coverage floor on golden records."""
    covered = {d.typename for d in report.diffs if d.golden_count > 0}
    absent = sorted(set(expect_typenames) - covered)
    if absent:
        raise AssertionError(
            "Golden corpus is missing expected typename(s): "
            f"{', '.join(absent)}. Expected {sorted(set(expect_typenames))}, "
            f"golden records present for {sorted(covered)}."
        )


def _callable_name(func: Callable[..., Any]) -> str:
    return getattr(func, "__name__", repr(func))


def _truncate(value: Any) -> str:
    text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        return text[: _MAX_VALUE_CHARS - 3] + "..."
    return text


def _format_keys(label: str, keys: tuple[str, ...]) -> list[str]:
    lines = [f"  {label} ({len(keys)}):"]
    for asset_key in keys[:_MAX_KEYS_SHOWN]:
        lines.append(f"    {asset_key}")
    if len(keys) > _MAX_KEYS_SHOWN:
        lines.append(f"    ... and {len(keys) - _MAX_KEYS_SHOWN} more")
    return lines


def _format_typename_detail(diff: TypenameDiff) -> list[str]:
    lines = [f"[{diff.typename}]"]

    if diff.extra_in_ours:
        lines.extend(_format_keys("extra in ours", diff.extra_in_ours))
    if diff.missing_in_ours:
        lines.extend(_format_keys("missing from ours", diff.missing_in_ours))

    if diff.mismatches:
        lines.append(f"  mismatched ({len(diff.mismatches)}):")
        for mismatch in diff.mismatches[:_MAX_MISMATCHES_SHOWN]:
            lines.append(f"    {mismatch.key}")
            for field_diff in mismatch.field_diffs[:_MAX_FIELDS_SHOWN]:
                lines.append(
                    f"      {field_diff.field_path}: "
                    f"golden={_truncate(field_diff.golden_value)} "
                    f"ours={_truncate(field_diff.ours_value)}"
                )
            remaining = len(mismatch.field_diffs) - _MAX_FIELDS_SHOWN
            if remaining > 0:
                lines.append(f"      ... and {remaining} more field(s)")
        remaining = len(diff.mismatches) - _MAX_MISMATCHES_SHOWN
        if remaining > 0:
            lines.append(f"    ... and {remaining} more asset(s)")

    if not diff.has_failures:
        lines.append("  (not gated by this typename's rule)")
    lines.append("")
    return lines


__all__ = [
    "NO_TYPENAME",
    "AssetMismatch",
    "DiffPolicy",
    "DuplicateKeyPolicy",
    "FieldDiff",
    "GoldenDuplicateKeyError",
    "GoldenReport",
    "GoldenRuleError",
    "TypenameDiff",
    "TypenameRule",
    "assert_matches_golden",
    "diff_golden",
]

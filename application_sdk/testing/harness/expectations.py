"""The two pure evaluators, generalised off the connector class attributes.

Both already exist on ``BaseE2ETest`` as pure functions of their input plus a
dozen ``ClassVar``\\s: ``_evaluate_asset_expectations`` (floors, exact parity,
and the "completed but extracted nothing" backstop) and
``_validate_asset_locations`` (sampled qualified names nested under the
connection at the declared depth). They are the two pieces of the harness that
already need no tenant to test — which is exactly why they lift first (child B
on FND-224).

Generalising means taking the declarations as an argument instead of reading
``self``. That is the whole change: a composer that is not a ``BaseE2ETest``
subclass can then evaluate the same expectations, and the connector's class
attributes become one way of *supplying* :class:`AssetExpectations` rather than
the only place it can live.

Both return findings rather than raising, for the accumulation reason in
:mod:`application_sdk.testing.harness.outcome`: a run reports every unmet
expectation, not the first one.

One behaviour is deliberately **not** preserved. ``_validate_asset_locations``
fails open today — the sampling read returns ``[]`` on any search error, which
arrives as "no samples, skip", so an auth fault reads as a pass. That is finding
C4 on FND-224, and the fix here is structural rather than documentary: a reading
that could not be taken is :class:`Unreadable`, a variant the input mapping
itself can hold, so "I could not read" can no longer be spelled the same way as
"nothing to check". Every consulted reading that is :class:`Unreadable` produces
a finding carrying :data:`UNREADABLE` as its expectation — a machine-readable
marker, so whoever assembles the verdict can grade it as
:class:`~application_sdk.testing.harness.outcome.Indeterminate` rather than as a
component regression, without parsing prose.

The count evaluator gets the same treatment, because it has the mirror-image
version of the same bug: an unreadable count arrives as ``0`` and is reported as
"asset floor not met" — fail-*closed*, but attributed to the connector instead
of to the search that failed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeAlias, Union

__all__ = [
    "UNREADABLE",
    "AssetExpectations",
    "CountRead",
    "Finding",
    "SampleRead",
    "Unreadable",
    "evaluate_counts",
    "evaluate_locations",
]

#: :attr:`Finding.expectation` value marking a finding that exists because a
#: reading could not be taken, not because an expectation was unmet. The one
#: value a caller must not grade as a component regression.
UNREADABLE = "unreadable"

#: Subject used for findings about the run as a whole rather than one type.
_ALL_TYPES = "all asset types"


@dataclass(frozen=True, slots=True, kw_only=True)
class Unreadable:
    """A reading that could not be taken.

    Exists so that "the search failed" has a spelling of its own. Without it the
    only vocabulary available to a failed read is the vocabulary of a successful
    one — an empty sample list, a zero count — and both of those already mean
    something else.

    Attributes:
        cause: The exception that made the reading unavailable. Retained rather
            than stringified, matching
            :class:`~application_sdk.testing.harness.outcome.Indeterminate`, so
            a caller can classify it without re-parsing a message.
    """

    cause: BaseException


#: One per-type count, or the fact that it could not be read.
CountRead: TypeAlias = Union[int, Unreadable]

#: One per-type sample of qualified names, or the fact that it could not be
#: read. ``Sequence[str]`` rather than ``list[str]`` so a caller may pass a tuple.
SampleRead: TypeAlias = Union[Sequence[str], Unreadable]


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One unmet expectation, in a form a report can render without re-parsing.

    A string was enough while the only consumer was a pytest failure message.
    It is not enough for the evidence bundle, which groups findings by what they
    are about.

    Attributes:
        subject: What the finding is about — an asset type name, a node name.
        detail: Human-readable statement of what was expected and what was seen.
            Written for whoever reads a red CI leg.
        expectation: Which declared expectation was not met, e.g. ``"floor"``,
            ``"exact"``, ``"nonempty"``, ``"depth"``, ``"nesting"`` — or
            :data:`UNREADABLE` when the reading itself was unavailable.
    """

    subject: str
    detail: str
    expectation: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetExpectations:
    """What a run is expected to have landed in Atlas.

    Attributes:
        floors: Asset type -> minimum count (``>=``).
        exacts: Asset type -> exact count (``==``), against a committed baseline
            from a direct (non-agent) run. Catches over-extraction as well as
            under-extraction, which a floor cannot.
        depths: Asset type -> number of qualified-name segments expected below
            the connection prefix. Catches assets that landed at the wrong
            hierarchy level — mis-parented, flattened, a dropped path segment —
            even when the count is right.
        require_nonempty: Whether a run that completes and lands zero assets
            fails. Defaults on, and it fires even for a connector that declares
            nothing else — those are the ones most likely to regress silently.
        connection_qualified_name: Prefix every sampled qualified name must sit
            under. Empty skips :func:`evaluate_locations` entirely: depth is
            measured *below this prefix*, so with no prefix there is nothing to
            measure from and no nesting to assert.
    """

    floors: Mapping[str, int] = field(default_factory=dict)
    exacts: Mapping[str, int] = field(default_factory=dict)
    depths: Mapping[str, int] = field(default_factory=dict)
    require_nonempty: bool = True
    connection_qualified_name: str = ""


def _unreadable(subject: str, reading: Unreadable, *, checking: str) -> Finding:
    """Build the finding for a reading that could not be taken.

    Args:
        subject: What the unavailable reading was about.
        reading: The failed read, carrying its cause.
        checking: The check that consulted it, named so the report says which
            expectation went ungraded rather than only that a read failed.

    Returns:
        A finding whose :attr:`Finding.expectation` is :data:`UNREADABLE`.
    """
    return Finding(
        subject=subject,
        detail=(
            f"could not be read, so the {checking} expectation was not graded: "
            f"{type(reading.cause).__name__}: {reading.cause}"
        ),
        expectation=UNREADABLE,
    )


def evaluate_counts(
    counts: Mapping[str, CountRead],
    expectations: AssetExpectations,
    *,
    total_assets: CountRead | None = None,
) -> Sequence[Finding]:
    """Evaluate per-type counts against the declared floors, exacts and backstop.

    Args:
        counts: Asset type -> count observed in Atlas, or :class:`Unreadable`
            when that count could not be read. A type absent from the mapping
            counts as zero, which is what "the search ran and found none" means;
            a type whose read *failed* must be present and :class:`Unreadable`.
        expectations: What was declared.
        total_assets: True count across *all* asset types, which is not
            ``sum(counts.values())`` when only some types were counted. The
            non-empty backstop reads this so it fires for a connector that
            declared no per-type expectations at all. ``None`` falls back to the
            sum of the per-type counts, which is what lets the evaluator be
            driven from a unit test — and that fallback is refused when any of
            them was unreadable, since a partial sum reading zero is exactly the
            fail-open this evaluator exists to close.

    Returns:
        One :class:`Finding` per unmet expectation, floors then exacts then the
        backstop; empty when all were met. A finding whose expectation is
        :data:`UNREADABLE` says the check could not be graded — never that the
        thing under test regressed.
    """
    findings: list[Finding] = []

    for type_name, floor in expectations.floors.items():
        got = counts.get(type_name, 0)
        if isinstance(got, Unreadable):
            findings.append(_unreadable(type_name, got, checking="floor"))
            continue
        if got < floor:
            findings.append(
                Finding(
                    subject=type_name,
                    detail=f"got {got}, expected >= {floor}",
                    expectation="floor",
                )
            )

    for type_name, want in expectations.exacts.items():
        got = counts.get(type_name, 0)
        if isinstance(got, Unreadable):
            findings.append(_unreadable(type_name, got, checking="exact"))
            continue
        if got != want:
            findings.append(
                Finding(
                    subject=type_name,
                    detail=(
                        f"got {got}, expected exactly {want} "
                        "(count parity vs. direct-run baseline)"
                    ),
                    expectation="exact",
                )
            )

    findings.extend(_evaluate_nonempty(counts, expectations, total_assets))
    return findings


def _evaluate_nonempty(
    counts: Mapping[str, CountRead],
    expectations: AssetExpectations,
    total_assets: CountRead | None,
) -> Sequence[Finding]:
    """Apply the "completed but extracted nothing" backstop.

    Args:
        counts: As passed to :func:`evaluate_counts`.
        expectations: What was declared.
        total_assets: The all-types total, or ``None`` to fall back to the sum
            of the per-type counts.

    Returns:
        At most one finding.
    """
    floors = expectations.floors
    exacts = expectations.exacts
    has_positive_expectation = any(value > 0 for value in floors.values()) or any(
        value > 0 for value in exacts.values()
    )
    # A connector whose only declared expectations are zero (e.g. exact
    # {"X": 0}) is asserting "produces zero of X" — the backstop must not
    # override that into a failure.
    asserting_zero = bool(floors or exacts) and not has_positive_expectation
    if not expectations.require_nonempty or asserting_zero:
        return ()

    total = total_assets
    if total is None:
        unreadable = next(
            (value for value in counts.values() if isinstance(value, Unreadable)),
            None,
        )
        # Summing around an unreadable count gives a total that is low by an
        # unknown amount — and the value it is compared against is zero, so
        # "low" is precisely the direction that turns a failed search into a
        # confident claim about the connector.
        total = (
            unreadable
            if unreadable is not None
            else sum(value for value in counts.values() if isinstance(value, int))
        )

    if isinstance(total, Unreadable):
        return (_unreadable(_ALL_TYPES, total, checking="non-empty"),)
    if total == 0:
        return (
            Finding(
                subject=_ALL_TYPES,
                detail=(
                    "run produced ZERO assets in Atlas (workflow completed but "
                    "extracted nothing)"
                ),
                expectation="nonempty",
            ),
        )
    return ()


def evaluate_locations(
    samples: Mapping[str, SampleRead],
    expectations: AssetExpectations,
) -> Sequence[Finding]:
    """Evaluate sampled qualified names against the declared hierarchy depths.

    Args:
        samples: Asset type -> sampled qualified names, or :class:`Unreadable`
            when the sample read failed. A type with an *empty* sample is
            skipped: "too few or none" is already covered by the count floors and
            the non-empty backstop, so this check is only about the *shape* of
            assets that did land. That skip is the reason a failed read may not
            be spelled as an empty sequence.
        expectations: What was declared. An empty
            :attr:`AssetExpectations.connection_qualified_name` makes this a
            no-op — see that attribute.

    Returns:
        One :class:`Finding` per sampled name that is not nested under the
        connection, or is nested at the wrong depth; empty when all were fine.
    """
    connection = expectations.connection_qualified_name
    findings: list[Finding] = []
    if not connection:
        return findings

    prefix = f"{connection}/"
    for type_name, depth in expectations.depths.items():
        sample = samples.get(type_name, ())
        if isinstance(sample, Unreadable):
            findings.append(_unreadable(type_name, sample, checking="depth"))
            continue
        for qualified_name in sample:
            findings.extend(
                _evaluate_one_location(
                    type_name, qualified_name, prefix=prefix, depth=depth
                )
            )
    return findings


def _evaluate_one_location(
    type_name: str, qualified_name: str, *, prefix: str, depth: int
) -> Sequence[Finding]:
    """Check one sampled qualified name against the connection prefix and depth.

    Args:
        type_name: Asset type the sample belongs to.
        qualified_name: The sampled qualified name.
        prefix: Connection qualified name with its trailing separator.
        depth: Segments the name must carry below ``prefix``.

    Returns:
        At most one finding.
    """
    if not qualified_name.startswith(prefix):
        return (
            Finding(
                subject=type_name,
                detail=(
                    f"{qualified_name!r} is not nested under the connection "
                    f"{prefix.rstrip('/')}"
                ),
                expectation="nesting",
            ),
        )
    # rstrip a trailing "/" first: a QN that ends in "/" would otherwise split
    # into an empty tail segment and over-count the depth by one. (Atlan QNs
    # conventionally don't end in "/", so this is defensive.)
    tail = qualified_name[len(prefix) :].rstrip("/")
    below = tail.split("/") if tail else []
    if len(below) != depth:
        return (
            Finding(
                subject=type_name,
                detail=(
                    f"{qualified_name!r} has {len(below)} segment(s) below the "
                    f"connection, expected {depth} (wrong hierarchy level)"
                ),
                expectation="depth",
            ),
        )
    return ()

"""The pure evaluators, generalised off the connector class attributes.

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

A third evaluator joined them on FND-2094: :func:`evaluate_attributes`. Counts
and depths together answer "did the right number of assets land, in the right
shape" and neither answers "does this asset carry the right values", so a
connector could publish a structurally perfect tree in which every computed
attribute was ``0`` and stay green. It inherits the same ``Unreadable``
contract, and adds one distinction of its own that the count checks structurally
cannot make: *present-but-zero* is not *absent*. See :class:`AssetAttributes`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeAlias, Union

__all__ = [
    "UNREADABLE",
    "Absent",
    "AssetAttributes",
    "AssetExpectations",
    "AtLeast",
    "AtMost",
    "AttributeExpectationValue",
    "AttributeMatcher",
    "AttributeSampleRead",
    "AttributeValue",
    "CountRead",
    "Exactly",
    "Finding",
    "Present",
    "SampleRead",
    "Unreadable",
    "as_matcher",
    "evaluate_attributes",
    "evaluate_counts",
    "evaluate_locations",
    "normalise_attribute_expectations",
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


# ---------------------------------------------------------------------------
# Attribute values, and the vocabulary for asserting on them
# ---------------------------------------------------------------------------
#
# Counts and depths say whether the right assets landed in the right shape.
# Neither says whether an asset carries the right *values*, so a connector can
# publish a structurally perfect tree in which every computed attribute is ``0``
# and every suite in the fleet stays green (FND-2094).
#
# Two distinctions have to survive from the Atlas read all the way into a
# finding, or the check reproduces the blind spot it exists to close:
#
# * **present-but-zero vs absent.** ``0`` is a value a connector meant to
#   publish; an absent attribute is one it stopped publishing. To a count
#   assertion they are the same and in Atlas they are not, which is why
#   :class:`AssetAttributes` spells absence as *the key is missing* rather than
#   as ``None`` — ``None`` is reserved for an attribute Atlas returned as null.
# * **unmet vs unreadable**, the same split the counts and samples already make.

#: One attribute value as Atlas returned it.
#:
#: ``object`` rather than ``Any`` on purpose: a matcher has to narrow before it
#: compares, and ``Any`` would let ``AtLeast(3).matches(present=True,
#: value="nine")`` type-check. The values that reach here are scalars — Atlas
#: indexes the computed attributes connectors assert on (``tableCount``,
#: ``schemaCount``, ``rowCount``, ``lastSyncRunAt``) as numbers, strings, bools
#: or nulls — but nothing stops a struct arriving, and a matcher that cannot
#: narrow one simply does not match it.
AttributeValue: TypeAlias = object

#: What a suite may write on the right-hand side of an attribute expectation: a
#: matcher, or a bare scalar meaning :class:`Exactly` that scalar. The sugar is
#: coerced to a matcher at the declaration boundary by :func:`as_matcher`, so
#: nothing past it handles two shapes.
AttributeExpectationValue: TypeAlias = Union[
    "AttributeMatcher", str, int, float, bool, None
]


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetAttributes:
    """The attribute values one sampled asset carries.

    Attributes:
        qualified_name: The sampled asset's qualifiedName, so a finding names
            the asset it is about and not only its type.
        values: Requested attribute name -> the value Atlas returned. **A
            requested attribute Atlas did not return is absent from this
            mapping**, which is how "the connector never set it" is spelled; a
            key present with ``None`` is an attribute Atlas returned as null.
            Collapsing those two into ``None`` would put the check back in the
            hole it was dug out of.
    """

    qualified_name: str
    values: Mapping[str, AttributeValue] = field(default_factory=dict)


#: One per-type sample of asset attributes, or the fact that it could not be
#: read. Same shape and same reason as :data:`SampleRead`.
AttributeSampleRead: TypeAlias = Union[Sequence[AssetAttributes], Unreadable]


class AttributeMatcher(ABC):
    """One claim about a single attribute value on a single asset.

    A closed vocabulary rather than ``Callable[[object], bool]``. A callable
    would be shorter to offer and worse to read: a red CI leg has to print *what
    was expected*, and a lambda can only print itself. Every matcher therefore
    answers two questions — did it match, and what was it asking for.

    Both halves of the reading are passed separately because the interesting
    case is the one where there is no value at all. ``present=False`` means
    Atlas returned no such attribute on this asset, and ``value`` then carries
    no information.
    """

    __slots__ = ()

    @abstractmethod
    def matches(self, *, present: bool, value: AttributeValue) -> bool:
        """Whether this reading satisfies the claim.

        Args:
            present: Whether Atlas returned the attribute at all.
            value: The value it returned. Meaningless when *present* is False.

        Returns:
            True when the claim holds.
        """

    @abstractmethod
    def describe(self) -> str:
        """The claim, as the ``expected ...`` half of a failure line.

        Returns:
            A short phrase, e.g. ``"exactly 8"`` or ``"a number >= 1"``.
        """


@dataclass(frozen=True, slots=True)
class Exactly(AttributeMatcher):
    """The attribute is present and equal to *expected*.

    The strictest matcher, and the right one against a pinned hermetic fixture
    where the number is knowable. Against a live source it is the wrong tool —
    :class:`AtLeast` or :class:`Present` belong there.

    Attributes:
        expected: The value the attribute must carry.
    """

    expected: AttributeValue

    def matches(self, *, present: bool, value: AttributeValue) -> bool:
        """Whether the attribute is present and equal to :attr:`expected`.

        Args:
            present: Whether Atlas returned the attribute.
            value: The value it returned.

        Returns:
            True when present and equal. ``0`` never matches ``False`` and ``1``
            never matches ``True``, though Python's ``==`` says they do: a flag
            and a count are different findings about a connector, and quietly
            equating them is the class of confusion this knob exists to remove.
        """
        if not present:
            return False
        if isinstance(value, bool) != isinstance(self.expected, bool):
            return False
        return bool(value == self.expected)

    def describe(self) -> str:
        """The claim as a phrase.

        Returns:
            ``"exactly <expected>"``.
        """
        return f"exactly {self.expected!r}"


@dataclass(frozen=True, slots=True)
class Present(AttributeMatcher):
    """The attribute is present and not null — any value will do.

    The matcher that catches the silent-drop regression: a connector that stops
    setting an attribute entirely reads as green to every count assertion, and
    as a failure here. Use it wherever the value depends on a live source and
    only its existence is pinnable.
    """

    def matches(self, *, present: bool, value: AttributeValue) -> bool:
        """Whether Atlas returned a non-null value.

        Args:
            present: Whether Atlas returned the attribute.
            value: The value it returned.

        Returns:
            True when present and not ``None``.
        """
        return present and value is not None

    def describe(self) -> str:
        """The claim as a phrase.

        Returns:
            ``"a value (present and not null)"``.
        """
        return "a value (present and not null)"


@dataclass(frozen=True, slots=True)
class Absent(AttributeMatcher):
    """The attribute is not set on this asset.

    The inverse pin, and not a curiosity: a connector may deliberately leave an
    attribute unset rather than publish ``0`` for it, because absent means
    "unknown" while ``0`` is a claim. This is the only way to assert that rule
    held.
    """

    def matches(self, *, present: bool, value: AttributeValue) -> bool:
        """Whether Atlas returned no such attribute.

        Args:
            present: Whether Atlas returned the attribute.
            value: Ignored.

        Returns:
            True when the attribute is absent. An attribute returned as ``None``
            counts as *present*: Atlas holding an explicit null is a different
            state from Atlas holding nothing, and a connector that published the
            first has not left the attribute unset.
        """
        return not present

    def describe(self) -> str:
        """The claim as a phrase.

        Returns:
            ``"no value (attribute unset)"``.
        """
        return "no value (attribute unset)"


@dataclass(frozen=True, slots=True)
class AtLeast(AttributeMatcher):
    """The attribute is a number greater than or equal to *minimum*.

    The live-source counterpart to :class:`Exactly`: a crawl of a real database
    cannot pin ``tableCount == 8``, but ``AtLeast(1)`` still separates "computed
    something" from "published the degraded zero".

    Attributes:
        minimum: The floor the value must clear.
    """

    minimum: float

    def matches(self, *, present: bool, value: AttributeValue) -> bool:
        """Whether the attribute is a number at or above :attr:`minimum`.

        Args:
            present: Whether Atlas returned the attribute.
            value: The value it returned.

        Returns:
            True when present and numerically at least :attr:`minimum`. A
            non-numeric value does not match — an attribute whose type changed
            is a finding, not a comparison error.
        """
        number = _as_number(present, value)
        return number is not None and number >= self.minimum

    def describe(self) -> str:
        """The claim as a phrase.

        Returns:
            ``"a number >= <minimum>"``.
        """
        return f"a number >= {self.minimum}"


@dataclass(frozen=True, slots=True)
class AtMost(AttributeMatcher):
    """The attribute is a number less than or equal to *maximum*.

    Attributes:
        maximum: The ceiling the value must stay under.
    """

    maximum: float

    def matches(self, *, present: bool, value: AttributeValue) -> bool:
        """Whether the attribute is a number at or below :attr:`maximum`.

        Args:
            present: Whether Atlas returned the attribute.
            value: The value it returned.

        Returns:
            True when present and numerically at most :attr:`maximum`.
        """
        number = _as_number(present, value)
        return number is not None and number <= self.maximum

    def describe(self) -> str:
        """The claim as a phrase.

        Returns:
            ``"a number <= <maximum>"``.
        """
        return f"a number <= {self.maximum}"


def _as_number(present: bool, value: AttributeValue) -> float | None:
    """Narrow a reading to a number, or say it is not one.

    Args:
        present: Whether Atlas returned the attribute.
        value: The value it returned.

    Returns:
        The value as a float, or ``None`` when it is absent or not a number.
        ``bool`` is excluded although Python calls it an ``int``: ``AtLeast(1)``
        matching ``True`` would be a coincidence of the type system rather than
        an assertion anyone wrote.
    """
    if not present or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def as_matcher(declared: AttributeExpectationValue) -> AttributeMatcher:
    """Coerce one declared right-hand side into a matcher.

    Args:
        declared: A matcher, or a bare scalar meaning :class:`Exactly` it.

    Returns:
        The matcher.
    """
    if isinstance(declared, AttributeMatcher):
        return declared
    return Exactly(declared)


def normalise_attribute_expectations(
    declared: Mapping[str, Mapping[str, AttributeExpectationValue]],
) -> dict[str, dict[str, AttributeMatcher]]:
    """Coerce a whole declaration into matchers.

    Args:
        declared: Asset type -> attribute name -> matcher or bare scalar, as a
            suite writes it.

    Returns:
        The same mapping with every value a matcher, so
        :func:`evaluate_attributes` never sees the sugar.
    """
    return {
        type_name: {
            attribute: as_matcher(value) for attribute, value in attributes.items()
        }
        for type_name, attributes in declared.items()
    }


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
        attributes: Asset type -> attribute name -> the claim its value must
            satisfy, already coerced to matchers by
            :func:`normalise_attribute_expectations`. Catches an asset that
            landed in the right place, in the right number, carrying the wrong
            *values* — a computed count degraded to ``0``, or an attribute that
            silently stopped being set. Counts and depths can see neither.
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
    attributes: Mapping[str, Mapping[str, AttributeMatcher]] = field(
        default_factory=dict
    )
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


def evaluate_attributes(
    samples: Mapping[str, AttributeSampleRead],
    expectations: AssetExpectations,
) -> Sequence[Finding]:
    """Evaluate sampled attribute values against the declared matchers.

    The third grader, and the only one that looks at what an asset *says* rather
    than at how many of them there are or where they sit. Every sampled asset of
    a declared type must satisfy every matcher declared for that type, so one
    mis-valued asset among the sample is a finding.

    Args:
        samples: Asset type -> the sampled assets and their attribute values, or
            :class:`Unreadable` when the sample read failed. A type with an
            *empty* sample is skipped, for the reason :func:`evaluate_locations`
            skips one: "too few or none" is the count floors' job, and this check
            is only about the values assets that did land are carrying. That skip
            is why an unreadable read may not be spelled as an empty sequence —
            and why every type declared here should be paired with a floor.
        expectations: What was declared.

    Returns:
        One :class:`Finding` per (asset, attribute) that did not satisfy its
        matcher; empty when all did. A finding whose expectation is
        :data:`UNREADABLE` says the check could not be graded.
    """
    findings: list[Finding] = []
    for type_name, matchers in expectations.attributes.items():
        sample = samples.get(type_name, ())
        if isinstance(sample, Unreadable):
            findings.append(_unreadable(type_name, sample, checking="attribute"))
            continue
        for asset in sample:
            findings.extend(_evaluate_one_asset(type_name, asset, matchers))
    return findings


def _evaluate_one_asset(
    type_name: str,
    asset: AssetAttributes,
    matchers: Mapping[str, AttributeMatcher],
) -> Sequence[Finding]:
    """Check one sampled asset against every matcher declared for its type.

    Args:
        type_name: Asset type the sample belongs to.
        asset: The sampled asset's qualified name and attribute values.
        matchers: Attribute name -> the claim its value must satisfy.

    Returns:
        One finding per unsatisfied matcher, in declaration order.
    """
    findings: list[Finding] = []
    for attribute, matcher in matchers.items():
        present = attribute in asset.values
        value = asset.values.get(attribute)
        if matcher.matches(present=present, value=value):
            continue
        # "is absent" rather than "= None": the whole point of carrying presence
        # separately is that a red leg says which of the two it was, since they
        # implicate different halves of a connector.
        observed = f"= {value!r}" if present else "is absent (attribute not set)"
        findings.append(
            Finding(
                subject=f"{type_name}.{attribute}",
                detail=(
                    f"on {asset.qualified_name!r} {observed}, "
                    f"expected {matcher.describe()}"
                ),
                expectation="attribute",
            )
        )
    return findings

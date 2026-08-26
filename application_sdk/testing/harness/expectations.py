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
C4 on FND-224, and the fix is structural: a caller that could not read passes
:class:`~application_sdk.testing.harness.outcome.Indeterminate` rather than an
empty sample set, so "unreadable" can no longer be spelled the same way as
"nothing to check".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = ["AssetExpectations", "Finding", "evaluate_counts", "evaluate_locations"]


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
            ``"exact"``, ``"nonempty"``, ``"depth"``.
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
            under. Empty means the nesting half of the location check is skipped.
    """

    floors: Mapping[str, int] = field(default_factory=dict)
    exacts: Mapping[str, int] = field(default_factory=dict)
    depths: Mapping[str, int] = field(default_factory=dict)
    require_nonempty: bool = True
    connection_qualified_name: str = ""


def evaluate_counts(
    counts: Mapping[str, int],
    expectations: AssetExpectations,
    *,
    total_assets: int | None = None,
) -> Sequence[Finding]:
    """Evaluate per-type counts against the declared floors, exacts and backstop.

    Args:
        counts: Asset type -> count observed in Atlas.
        expectations: What was declared.
        total_assets: True count across *all* asset types, which is not
            ``sum(counts.values())`` when only some types were counted. The
            non-empty backstop reads this so it fires for a connector that
            declared no per-type expectations at all. ``None`` falls back to the
            sum, which is what lets the evaluator be driven from a unit test.

    Returns:
        One :class:`Finding` per unmet expectation; empty when all were met.

    Raises:
        NotImplementedError: Always — implementation is child B on FND-224.
    """
    raise NotImplementedError("evaluate_counts is child B on FND-224")


def evaluate_locations(
    samples: Mapping[str, Sequence[str]],
    expectations: AssetExpectations,
) -> Sequence[Finding]:
    """Evaluate sampled qualified names against the declared hierarchy depths.

    Args:
        samples: Asset type -> sampled qualified names. A type with no samples is
            skipped: "too few or none" is already covered by the count floors and
            the non-empty backstop, so this check is only about the *shape* of
            assets that did land. Callers must not spell "I could not read" as an
            empty mapping — see the module docstring.
        expectations: What was declared.

    Returns:
        One :class:`Finding` per sampled name that is not nested under the
        connection, or is nested at the wrong depth; empty when all were fine.

    Raises:
        NotImplementedError: Always — implementation is child B on FND-224.
    """
    raise NotImplementedError("evaluate_locations is child B on FND-224")

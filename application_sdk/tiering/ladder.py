"""Size-tier ladders: decide which worker pool one activity should run on.

Pure: no I/O, no environment reads, no clock, no SDK imports. Every function
here is deterministic and safe to call from Temporal workflow code, which may do
none of those things. The package re-exports it lazily for the same reason, so
``from application_sdk.tiering import Ladder`` in a workflow module does not
drag the activity-side half of the package in behind it.

**Every sizer is total: it always names a tier.** There is no "unclassified"
outcome, because an activity with no tier is an activity the OOM lane cannot
rescue — ``temporal-activity-rerouter`` only escalates *from* a pool that is a
declared tier, so anything left on the default queue is on its own the moment it
is killed. Totality is what makes escalation cover the whole app.

Fail-open therefore lives one layer down, in :mod:`application_sdk.tiering.queues`:
with ``ATLAN_APP_SIZE_QUEUE_MAP`` unset, ``resolve`` answers ``None`` for every
tier and every activity keeps the queue it has today. That is the inert build,
and it is a property of the deployment rather than of the classification.

**A malformed ladder raises at construction**, because every way of getting one
wrong is silent at runtime: a floor naming a tier the ladder does not declare
sizes nothing and routes nowhere, bands out of order make the later ones
unreachable, and a sizer keyed under one name while declaring another drifts
the moment somebody renames an activity. None of that shows up as an error — it
shows up as work on the wrong pool, which is what the ladder exists to prevent.

Those raise ``ValueError`` rather than an SDK error leaf, deliberately. The
categorical leaves live under ``application_sdk.errors``, whose package import
pulls in ``httpx``; this module is imported by workflow code to ask for a tier
inline, so it stays on the standard library. A ladder that fails to construct
also never travels — it stops the worker at import, which is the whole point —
so it has no need of the error envelope's audience and category routing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from math import inf, isfinite, isnan

Tier = str

#: Convenience for the open-ended top band.
INF = inf


@dataclass(frozen=True)
class Band:
    """Everything up to and including ``upper`` on one axis belongs to ``tier``."""

    upper: float
    tier: Tier


@dataclass(frozen=True)
class Axis:
    """One measurable dimension of the work, cut into ascending tier bands.

    ``name`` is the key a sizer reads out of the signal mapping. Bands must be
    ascending by ``upper``; the last one is normally ``Band(INF, ...)`` so the
    axis is total over non-negative finite values.

    An axis may still answer ``None`` — that means *this run did not measure
    this dimension*, not *this run is small*. The activity's floor covers that
    case, which is why an unmeasured axis can never drag a tier downwards.
    """

    name: str
    bands: tuple[Band, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Axis.name must not be empty")
        if not self.bands:
            raise ValueError(f"Axis {self.name!r} declares no bands")
        uppers = [band.upper for band in self.bands]
        for upper in uppers:
            # NaN compares False against everything, so a NaN bound silently
            # sends every value past it to the last band; a negative bound can
            # never match a measurement and is dead weight.
            if isnan(upper) or upper < 0:
                raise ValueError(
                    f"Axis {self.name!r} has band bound {upper!r}; bounds must be "
                    "non-negative (INF is allowed for the open-ended top band)"
                )
        if uppers != sorted(uppers) or len(set(uppers)) != len(uppers):
            raise ValueError(
                f"Axis {self.name!r} bands must ascend by upper bound, got {uppers}. "
                "tier_for takes the first band a value fits, so an out-of-order "
                "or duplicated bound makes the later band unreachable."
            )
        for band in self.bands:
            if not band.tier:
                raise ValueError(f"Axis {self.name!r} has a band with no tier")

    def tier_for(self, value: float | None) -> Tier | None:
        """The tier this axis asks for, or ``None`` when nothing was measured.

        ``None``, a NaN/inf reading and a negative value are all "not measured".
        Zero is a real measurement — an empty run genuinely belongs in the
        smallest band — which is why a sizer must *omit* a key it does not know
        rather than passing ``0``.
        """
        if value is None or not isfinite(value) or value < 0:
            return None
        for band in self.bands:
            if value <= band.upper:
                return band.tier
        # Past the top band's bound: the top rung is the answer. Only reachable
        # when the last band is finite; the usual Band(INF, ...) absorbs it.
        return self.bands[-1].tier


@dataclass(frozen=True)
class ActivitySizer:
    """What tier one activity needs. **Total** — it always returns a tier.

    ``floor`` is where the activity starts when no axis measures anything, and
    it is also a lower bound when they do: an axis may raise a run above the
    floor, never below it. Pin-and-climb is therefore the shape of every sizer
    that declares no axis at all, and adding an axis is a strict widening of the
    cases in which the activity starts higher.

    ``why`` is not decoration. It records what the measurement this floor came
    from actually said, so the next person retuning it can tell a measured floor
    from a guessed one.
    """

    activity: str
    floor: Tier
    why: str
    axes: tuple[Axis, ...] = ()
    #: Set when the floor is a placeholder rather than a measurement — too few
    #: executions were observed to say anything. Report it from a guard test so
    #: these decay visibly instead of silently hardening into facts.
    provisional: bool = False

    def __post_init__(self) -> None:
        if not self.activity:
            raise ValueError("ActivitySizer.activity must not be empty")
        if not self.floor:
            raise ValueError(f"Sizer for {self.activity!r} has no floor tier")
        if not self.why:
            raise ValueError(
                f"Sizer for {self.activity!r} must say why: the floor is a measurement, "
                "and the next person retuning it needs to tell a measured floor from a guess."
            )
        names = [axis.name for axis in self.axes]
        if len(set(names)) != len(names):
            raise ValueError(
                f"Sizer for {self.activity!r} declares axis {names} more than once"
            )

    def tier_for(self, signal: Mapping[str, float], order: Sequence[Tier]) -> Tier:
        """The highest of ``floor`` and every tier a *measured* axis asks for.

        Axes absent from ``signal`` are skipped, not treated as zero. Taking the
        maximum across axes is deliberate: a single axis under-sizes real
        workloads — on one app's fleet, 3 of 160 tenants had memory tracking row
        *width* rather than row count, and a rows-only ladder put them two tiers
        low.
        """
        rank = {tier: i for i, tier in enumerate(order)}
        best = self.floor
        for axis in self.axes:
            asked = axis.tier_for(signal.get(axis.name))
            if asked is not None and rank.get(asked, -1) > rank.get(best, -1):
                best = asked
        return best


@dataclass(frozen=True)
class Ladder:
    """An app's tiers, in ascending capability, plus one sizer per activity.

    ``memory_gib`` is the declared envelope per tier. It is what :meth:`next_up`
    reasons about and what ``temporal-activity-rerouter`` re-derives from the
    pool's ``spec.template``, so it must match the pool resources the app
    declares in its deployment manifest — an app-side guard test should fail
    when the two drift.
    """

    order: tuple[Tier, ...]
    memory_gib: Mapping[Tier, float]
    sizers: Mapping[str, ActivitySizer]
    #: Answer for an activity with no registered sizer. Left unset it resolves
    #: to ``order[0]`` — the smallest rung, whatever this app calls its tiers —
    #: so a newly added activity is pinned low and climbs rather than reserving
    #: the fleet's worst envelope on its first run. An app-side
    #: ``test_every_task_has_a_sizer`` is what stops that fallback from becoming
    #: the normal path. Unset is ``""`` rather than ``None`` so the attribute is
    #: a ``Tier`` for every reader after construction, which is what lets
    #: :meth:`tier_for` promise it never answers ``None``.
    unknown_activity_tier: Tier = ""

    def __post_init__(self) -> None:
        if not self.order:
            raise ValueError("Ladder.order must declare at least one tier")
        if len(set(self.order)) != len(self.order):
            raise ValueError(f"Ladder.order repeats a tier: {self.order}")
        known = set(self.order)

        missing_memory = [tier for tier in self.order if tier not in self.memory_gib]
        if missing_memory:
            raise ValueError(
                f"Ladder.memory_gib is missing {missing_memory}. next_up orders escalation by "
                "declared memory, so an absent tier reads as 0 GiB and is escalated to first."
            )

        if not self.unknown_activity_tier:
            # Frozen dataclass: this is the documented way to normalise a field
            # in __post_init__, and it keeps the attribute a plain Tier for
            # every reader afterwards.
            object.__setattr__(self, "unknown_activity_tier", self.order[0])
        elif self.unknown_activity_tier not in known:
            raise ValueError(
                f"Ladder.unknown_activity_tier {self.unknown_activity_tier!r} is not in "
                f"{self.order}. Every unregistered activity would resolve to no queue and "
                "silently stay untiered."
            )

        for key, sizer in self.sizers.items():
            if key != sizer.activity:
                raise ValueError(
                    f"Ladder.sizers key {key!r} does not match its sizer's activity "
                    f"{sizer.activity!r}. Lookup goes by key, so the two drift apart silently."
                )
            if sizer.floor not in known:
                raise ValueError(
                    f"Sizer for {key!r} has floor {sizer.floor!r}, which is not in {self.order}"
                )
            for axis in sizer.axes:
                unknown = sorted({band.tier for band in axis.bands} - known)
                if unknown:
                    raise ValueError(
                        f"Axis {axis.name!r} on {key!r} names {unknown}, not in {self.order}. "
                        "An axis asking for a tier the ladder does not declare is ignored, so "
                        "the activity would quietly stay on its floor."
                    )

    def rank(self, tier: Tier) -> int:
        """Position in ``order``. Higher means more capable.

        Raises:
            ValueError: if ``tier`` is not one this ladder declares. Pools
                outside the ladder have no rank — ``next_up`` answers ``None``
                for them rather than asking.
        """
        try:
            return self.order.index(tier)
        except ValueError:
            raise ValueError(
                f"{tier!r} is not a tier of this ladder {self.order}"
            ) from None

    def tier_for(
        self, activity: str, signal: Mapping[str, float] | None = None
    ) -> Tier:
        """The tier ``activity`` should start on. **Never ``None``.**

        ``activity`` is the bare task name (``"write_output"``), not Temporal's
        qualified activity name.

        The answer is a function of this run's measured inputs and nothing else.
        There is deliberately no memory parameter: a ladder that could be raised
        by "where this connection ended last time" hides a broken axis instead of
        failing it. In a prior incident a byte-based axis answered the floor for
        an input that needed the top rung, and a per-connection tier memory
        carried those runs for three weeks — a ladder that looked like it worked
        and a sizing function nobody had reason to fix. A wrong answer here has
        to cost a visible climb, because the climb is the only feedback the
        ladder gets.
        """
        sizer = self.sizers.get(activity)
        return (
            self.unknown_activity_tier
            if sizer is None
            else sizer.tier_for(signal or {}, self.order)
        )

    def next_up(self, current: Tier, min_growth: float = 2.0) -> Tier | None:
        """The smallest tier worth escalating to, or ``None`` at the top.

        Mirrors ``NextTier`` in the rerouter so the app and the watcher agree
        about what the ladder means. Not simply the adjacent rung: moving an
        activity costs a whole pod and a cold start, so the target has to hold at
        least ``min_growth`` times the current memory to be worth it. When
        nothing clears that factor the adjacent rung is taken anyway — a hard
        filter would strand the second-largest tier whenever ``min_growth × its
        size`` exceeds the top.

        A tier not in ``order`` yields ``None``: pools that are not part of this
        ladder (the default worker, anything VPA-managed) must never be escalated
        *from*, because their declared limit is not their real one.
        """
        if current not in self.order:
            return None
        above = self.order[self.rank(current) + 1 :]
        if not above:
            return None
        floor = self.memory_gib.get(current, 0.0) * max(1.0, min_growth)
        for tier in above:
            if self.memory_gib.get(tier, 0.0) >= floor:
                return tier
        return above[0]

    def climb(
        self, start: Tier, *, steps: int = 3, min_growth: float = 2.0
    ) -> Iterator[Tier]:
        """The tiers this activity would attempt, in order: the start then its climbs.

        Only used offline — the app never escalates, the rerouter does — but
        scoring a proposed ladder means asking how many pods a miss costs, and
        that question has to be answered against the same ``next_up`` the
        watcher will use.
        """
        tier: Tier | None = start
        yield tier
        for _ in range(steps):
            tier = self.next_up(tier, min_growth)
            if tier is None:
                return
            yield tier

    def declared_tiers(self) -> Sequence[Tier]:
        """The ladder's tiers, ascending. Handy for guard tests."""
        return self.order

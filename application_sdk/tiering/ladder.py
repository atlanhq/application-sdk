"""Size-tier ladders: decide which worker pool one activity should run on.

Pure and import-light — no I/O, no environment reads, no SDK imports — so every
function here is safe to call from Temporal workflow code, where the sandbox
forbids both.

**Every sizer is total: it always names a tier.** There is no "unclassified"
outcome, because an activity with no tier is an activity the OOM lane cannot
rescue — ``temporal-activity-rerouter`` only escalates *from* a pool that is a
declared tier, so anything left on the default queue is on its own the moment it
is killed. Totality is what makes escalation cover the whole app.

Fail-open therefore lives one layer down, in :mod:`application_sdk.tiering.queues`:
with ``ATLAN_APP_SIZE_QUEUE_MAP`` unset, ``resolve`` answers ``None`` for every
tier and every activity keeps the queue it has today. That is the inert build,
and it is a property of the deployment rather than of the classification.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from math import inf, isfinite

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
        return self.bands[-1].tier if self.bands else None


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
    #: Answer for an activity with no registered sizer. The smallest rung, so a
    #: newly added activity is pinned low and climbs rather than reserving the
    #: fleet's worst envelope on its first run. An app-side
    #: ``test_every_task_has_a_sizer`` is what stops that fallback from becoming
    #: the normal path.
    unknown_activity_tier: Tier = field(default="s")

    def rank(self, tier: Tier) -> int:
        """Position in ``order``. Higher means more capable."""
        return self.order.index(tier)

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

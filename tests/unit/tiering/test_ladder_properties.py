"""Invariants that must hold for every ladder, not just the ones in the examples."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from application_sdk.tiering.ladder import INF, ActivitySizer, Axis, Band, Ladder
from application_sdk.tiering.queues import queue_map_from_env, resolve, tier_of_queue

tier_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=8
)
activity_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12
)
measurements = st.floats(
    min_value=0, max_value=1e12, allow_nan=False, allow_infinity=False
)
# Sampled values make equal neighbours common, not a rare float collision.
memory_values = st.one_of(
    st.sampled_from((1.0, 1.5, 8.0, 20.0)), st.floats(min_value=0.5, max_value=64)
)
estimates_mib = st.one_of(
    st.none(), st.floats(min_value=-1, max_value=1e6, allow_nan=False)
)


@st.composite
def ladders(draw: st.DrawFn) -> Ladder:
    """A ladder that satisfies every construction rule, so tests exercise behaviour."""
    order = tuple(draw(st.lists(tier_names, min_size=1, max_size=5, unique=True)))
    # Sorted, not strictly ascending: memory must not decrease along the order.
    sizes = sorted(
        draw(st.lists(memory_values, min_size=len(order), max_size=len(order)))
    )
    memory = dict(zip(order, sizes))

    sizers: dict[str, ActivitySizer] = {}
    for activity in draw(st.lists(activity_names, max_size=4, unique=True)):
        axes: list[Axis] = []
        for axis_name in draw(st.lists(activity_names, max_size=2, unique=True)):
            bounds = sorted(
                draw(st.lists(measurements, min_size=1, max_size=3, unique=True))
            )
            bands = tuple(
                Band(bound, draw(st.sampled_from(order))) for bound in bounds
            ) + (Band(INF, draw(st.sampled_from(order))),)
            axes.append(Axis(name=axis_name, bands=bands))
        sizers[activity] = ActivitySizer(
            activity=activity,
            floor=draw(st.sampled_from(order)),
            why="generated",
            axes=tuple(axes),
        )
    return Ladder(order=order, memory_gib=memory, sizers=sizers)


class TestLadderInvariants:
    @given(
        ladder=ladders(),
        activity=activity_names,
        signal=st.dictionaries(activity_names, measurements),
    )
    @settings(max_examples=200)
    def test_classification_is_total(
        self, ladder: Ladder, activity: str, signal: dict[str, float]
    ) -> None:
        """Whatever it is asked, the answer is a tier this ladder declares."""
        assert ladder.tier_for(activity, signal) in ladder.order

    @given(ladder=ladders(), signal=st.dictionaries(activity_names, measurements))
    @settings(max_examples=200)
    def test_an_axis_never_lowers_an_activity_below_its_floor(
        self, ladder: Ladder, signal: dict[str, float]
    ) -> None:
        for activity, sizer in ladder.sizers.items():
            assert ladder.rank(ladder.tier_for(activity, signal)) >= ladder.rank(
                sizer.floor
            )

    @given(ladder=ladders())
    @settings(max_examples=200)
    def test_escalation_only_ever_goes_up(self, ladder: Ladder) -> None:
        for tier in ladder.order:
            nxt = ladder.next_up(tier)
            assert nxt is None or ladder.rank(nxt) > ladder.rank(tier)

    @given(ladder=ladders())
    @settings(max_examples=200)
    def test_the_top_rung_never_escalates(self, ladder: Ladder) -> None:
        assert ladder.next_up(ladder.order[-1]) is None

    @given(ladder=ladders())
    @settings(max_examples=200)
    def test_a_climb_is_strictly_ascending_and_terminates(self, ladder: Ladder) -> None:
        for tier in ladder.order:
            climbed = list(ladder.climb(tier, steps=10))
            ranks = [ladder.rank(t) for t in climbed]
            assert ranks == sorted(set(ranks))
            assert len(climbed) <= len(ladder.order)

    @given(ladder=ladders(), activity=activity_names)
    @settings(max_examples=200)
    def test_an_unmeasured_run_is_the_floor(
        self, ladder: Ladder, activity: str
    ) -> None:
        """No signal at all must never resolve higher than the declared floor."""
        expected = (
            ladder.sizers[activity].floor
            if activity in ladder.sizers
            else ladder.unknown_activity_tier
        )
        assert ladder.tier_for(activity) == expected

    @given(
        ladder=ladders(),
        mib=estimates_mib,
        safety=st.floats(min_value=0, max_value=4, allow_nan=False),
    )
    @settings(max_examples=200)
    def test_tier_for_memory_is_the_smallest_tier_that_holds_it(
        self, ladder: Ladder, mib: float | None, safety: float
    ) -> None:
        tier = ladder.tier_for_memory(mib, safety=safety)
        if mib is None or mib <= 0:
            assert tier is None
            return
        required = mib * max(safety, 1.0)
        holds = [t for t in ladder.order if ladder.memory_gib[t] * 1024 >= required]
        assert tier == (holds[0] if holds else ladder.order[-1])

    @given(ladder=ladders(), data=st.data())
    @settings(max_examples=200)
    def test_cap_is_the_lower_of_the_tier_and_the_ceiling(
        self, ladder: Ladder, data: st.DataObject
    ) -> None:
        tier = data.draw(st.sampled_from(ladder.order))
        ceiling = data.draw(st.sampled_from(ladder.order))
        capped = ladder.cap(tier, ceiling)
        assert ladder.rank(capped) == min(ladder.rank(tier), ladder.rank(ceiling))

    @given(ladder=ladders(), data=st.data())
    @settings(max_examples=200)
    def test_highest_is_one_of_its_answers_and_at_least_each(
        self, ladder: Ladder, data: st.DataObject
    ) -> None:
        first = data.draw(st.sampled_from(ladder.order))
        others = data.draw(
            st.lists(st.one_of(st.none(), st.sampled_from(ladder.order)), max_size=3)
        )
        best = ladder.highest(first, *others)
        answers = [first, *(t for t in others if t)]
        assert best in answers
        assert all(ladder.rank(best) >= ladder.rank(t) for t in answers)


class TestQueueInvariants:
    @given(tier=tier_names)
    def test_an_empty_map_always_declines(self, tier: str) -> None:
        """The inert build, for every tier anyone could name."""
        assert resolve(tier, {}) is None

    @given(mapping=st.dictionaries(tier_names, tier_names, min_size=1, max_size=5))
    def test_resolve_and_tier_of_queue_round_trip(
        self, mapping: dict[str, str]
    ) -> None:
        """Only where queues are distinct — a shared queue has no single inverse."""
        queues = {tier: queue for tier, queue in mapping.items()}
        if len(set(queues.values())) != len(queues):
            return
        for tier in queues:
            assert tier_of_queue(resolve(tier, queues), queues) == tier

    @given(mapping=st.dictionaries(tier_names, tier_names, min_size=1, max_size=5))
    def test_a_valid_map_survives_the_environment(
        self, mapping: dict[str, str]
    ) -> None:
        """Anything this module would accept, it reads back unchanged."""
        import os

        os.environ["ATLAN_APP_SIZE_QUEUE_MAP"] = json.dumps(mapping)
        try:
            assert queue_map_from_env() == mapping
        finally:
            os.environ.pop("ATLAN_APP_SIZE_QUEUE_MAP", None)

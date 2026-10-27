"""The ladder is pure, total, and has no memory of previous runs."""

from __future__ import annotations

import pytest

from application_sdk.tiering.ladder import INF, ActivitySizer, Axis, Band, Ladder

ROWS = Axis(name="rows", bands=(Band(1_000, "s"), Band(100_000, "m"), Band(INF, "l")))
WIDTH = Axis(name="width", bands=(Band(10, "s"), Band(INF, "xl")))

LADDER = Ladder(
    order=("s", "m", "l", "xl"),
    memory_gib={"s": 6.0, "m": 8.0, "l": 16.0, "xl": 24.0},
    sizers={
        "pinned": ActivitySizer(
            activity="pinned", floor="m", why="measured: steady at 7 GiB"
        ),
        "sized": ActivitySizer(
            activity="sized", floor="s", why="measured: input-driven", axes=(ROWS,)
        ),
        "two_axes": ActivitySizer(
            activity="two_axes",
            floor="s",
            why="measured: width matters too",
            axes=(ROWS, WIDTH),
        ),
    },
)


class TestAxis:
    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            (0, "s"),
            (1_000, "s"),
            (1_001, "m"),
            (100_000, "m"),
            (100_001, "l"),
            (10**12, "l"),
        ],
    )
    def test_bands_are_inclusive_upper_bounds(self, rows: float, expected: str) -> None:
        assert ROWS.tier_for(rows) == expected

    @pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1])
    def test_an_unreadable_measurement_is_not_a_small_one(
        self, value: float | None
    ) -> None:
        """None/NaN/inf/negative all mean *not measured*, never "it is small"."""
        assert ROWS.tier_for(value) is None

    def test_zero_is_a_real_measurement(self) -> None:
        """An empty run genuinely belongs in the smallest band.

        This is why a sizer must *omit* a key it does not know rather than
        passing 0 — the two cases are distinguishable here and must stay so.
        """
        assert ROWS.tier_for(0) == "s"

    def test_an_axis_with_no_bands_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="declares no bands"):
            Axis(name="empty", bands=())


class TestActivitySizer:
    def test_a_sizer_with_no_axis_is_its_floor(self) -> None:
        assert LADDER.sizers["pinned"].tier_for({}, LADDER.order) == "m"

    def test_an_axis_raises_above_the_floor(self) -> None:
        assert LADDER.sizers["sized"].tier_for({"rows": 500_000}, LADDER.order) == "l"

    def test_an_axis_never_drags_below_the_floor(self) -> None:
        """A tiny measured input on a high-floor activity keeps the floor."""
        sizer = ActivitySizer(activity="x", floor="l", why="measured", axes=(ROWS,))
        assert sizer.tier_for({"rows": 1}, LADDER.order) == "l"

    def test_an_unmeasured_axis_is_skipped_not_zeroed(self) -> None:
        assert LADDER.sizers["sized"].tier_for({}, LADDER.order) == "s"

    def test_the_highest_asking_axis_wins(self) -> None:
        """Rows say 's', width says 'xl' — a rows-only ladder would under-size."""
        assert (
            LADDER.sizers["two_axes"].tier_for(
                {"rows": 10, "width": 5_000}, LADDER.order
            )
            == "xl"
        )

    def test_a_tier_outside_the_order_cannot_win(self) -> None:
        """An axis naming a tier this ladder does not declare is ignored."""
        rogue = Axis(name="rows", bands=(Band(INF, "enormous"),))
        sizer = ActivitySizer(activity="x", floor="s", why="measured", axes=(rogue,))
        assert sizer.tier_for({"rows": 10}, LADDER.order) == "s"


class TestLadder:
    def test_classification_is_total(self) -> None:
        """Every activity names a tier — there is no unclassified outcome.

        An activity with no tier is one the rerouter will not escalate, so
        totality here is what makes the OOM lane cover the whole app.
        """
        for activity in ("pinned", "sized", "two_axes", "never-registered"):
            assert LADDER.tier_for(activity) in LADDER.order

    def test_an_unregistered_activity_starts_on_the_smallest_rung(self) -> None:
        assert LADDER.tier_for("brand-new-task") == "s"

    def test_tier_for_reads_only_this_run(self) -> None:
        """Same activity, same signal, same answer — no per-connection memory.

        A ladder that could be raised by "where this connection ended last time"
        hides a broken axis instead of failing it. A wrong tier has to cost a
        visible climb.
        """
        first = LADDER.tier_for("sized", {"rows": 10})
        LADDER.tier_for("sized", {"rows": 10**9})
        assert LADDER.tier_for("sized", {"rows": 10}) == first == "s"

    def test_next_up_skips_a_rung_that_is_not_worth_a_pod(self) -> None:
        """s(6) needs >=12 to be worth moving: m(8) does not qualify, l(16) does."""
        assert LADDER.next_up("s") == "l"

    def test_next_up_falls_back_to_the_adjacent_rung(self) -> None:
        """Nothing clears 2x l(16)=32, so the top is taken rather than stranding l."""
        assert LADDER.next_up("l") == "xl"

    def test_the_top_has_nowhere_to_go(self) -> None:
        assert LADDER.next_up("xl") is None

    def test_a_pool_outside_the_ladder_is_never_escalated_from(self) -> None:
        """The default worker and VPA-managed pools: their declared limit is not real."""
        assert LADDER.next_up("default") is None

    def test_climb_is_the_sequence_of_attempts(self) -> None:
        assert list(LADDER.climb("s")) == ["s", "l", "xl"]

    def test_climb_stops_at_the_top(self) -> None:
        assert list(LADDER.climb("xl")) == ["xl"]

    def test_rank_orders_by_capability(self) -> None:
        assert (
            LADDER.rank("s") < LADDER.rank("m") < LADDER.rank("l") < LADDER.rank("xl")
        )

    def test_declared_tiers_are_ascending(self) -> None:
        assert tuple(LADDER.declared_tiers()) == LADDER.order


# Two rungs share 1 GiB and differ only in CPU, as in a real deployment.
EQUAL_NEIGHBOURS = Ladder(
    order=("xxs", "xs", "s", "m"),
    memory_gib={"xxs": 1.0, "xs": 1.0, "s": 1.5, "m": 8.0},
    sizers={},
)


class TestTierForMemory:
    """LADDER holds s=6144, m=8192, l=16384, xl=24576 MiB."""

    @pytest.mark.parametrize(
        ("mib", "expected"),
        [(1, "s"), (6_144, "s"), (6_145, "m"), (8_192, "m"), (16_000, "l")],
    )
    def test_the_smallest_tier_that_holds_the_estimate(
        self, mib: float, expected: str
    ) -> None:
        """An exact fit fits: the bound is inclusive, like a band's."""
        assert LADDER.tier_for_memory(mib) == expected

    def test_safety_scales_the_estimate_before_the_pick(self) -> None:
        """5,000 MiB fits s; x1.25 = 6,250 MiB does not."""
        assert LADDER.tier_for_memory(5_000) == "s"
        assert LADDER.tier_for_memory(5_000, safety=1.25) == "m"

    @pytest.mark.parametrize("safety", [0.5, 0.0, -2.0, float("nan")])
    def test_a_safety_below_one_cannot_shrink_the_estimate(self, safety: float) -> None:
        """8,000 MiB at x0.5 would be s; it is read as x1 and stays m."""
        assert LADDER.tier_for_memory(8_000, safety=safety) == "m"

    @pytest.mark.parametrize(("mib", "safety"), [(100_000, 1.0), (24_576, 1.25)])
    def test_nothing_fits_answers_the_top_tier(self, mib: float, safety: float) -> None:
        """Not None and not a raise: the largest pool is the best start there is."""
        assert LADDER.tier_for_memory(mib, safety=safety) == "xl"

    @pytest.mark.parametrize("mib", [None, 0, -1, float("nan"), float("inf")])
    def test_an_unusable_estimate_is_no_answer(self, mib: float | None) -> None:
        """Zero is not a reading here: no process runs in 0 MiB."""
        assert LADDER.tier_for_memory(mib) is None

    def test_equal_neighbours_pick_the_lower_rung(self) -> None:
        assert EQUAL_NEIGHBOURS.tier_for_memory(900) == "xxs"
        assert EQUAL_NEIGHBOURS.tier_for_memory(1_024) == "xxs"
        assert EQUAL_NEIGHBOURS.tier_for_memory(1_025) == "s"


class TestHighest:
    def test_the_more_capable_tier_wins_in_either_order(self) -> None:
        assert LADDER.highest("s", "l") == "l"
        assert LADDER.highest("l", "s") == "l"
        assert LADDER.highest("m") == "m"

    @pytest.mark.parametrize("missing", [None, ""])
    def test_a_missing_answer_is_skipped(self, missing: str | None) -> None:
        assert LADDER.highest("m", missing) == "m"

    def test_an_undeclared_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="not a tier of this ladder"):
            LADDER.highest("s", "huge")
        with pytest.raises(ValueError, match="not a tier of this ladder"):
            LADDER.highest("huge")

    @pytest.mark.parametrize(
        ("rows", "mib", "expected"),
        [
            (500_000, 1_000, "l"),  # bands ask more
            (10, 10_000, "l"),  # memory asks more
            (10, None, "s"),  # no estimate: the bands' answer stands
            (2_000, 7_000, "m"),  # both agree
        ],
    )
    def test_the_higher_of_the_size_bands_and_the_memory_estimate(
        self, rows: int, mib: float | None, expected: str
    ) -> None:
        tier = LADDER.highest(
            LADDER.tier_for("sized", {"rows": rows}), LADDER.tier_for_memory(mib)
        )
        assert tier == expected


class TestCap:
    @pytest.mark.parametrize(
        ("ceiling", "expected"),
        [("m", "m"), ("l", "l"), ("xl", "l")],
        ids=["below", "equal", "above"],
    )
    def test_a_ceiling_relative_to_the_natural_tier(
        self, ceiling: str, expected: str
    ) -> None:
        natural = LADDER.tier_for("sized", {"rows": 500_000})
        assert natural == "l"
        assert LADDER.cap(natural, ceiling) == expected

    def test_a_loose_estimate_takes_a_lower_ceiling(self) -> None:
        """The caller picks the ceiling: l when the estimate is tight, m when loose."""
        tier = LADDER.highest(
            LADDER.tier_for("sized", {"rows": 10}),
            LADDER.tier_for_memory(12_000, safety=1.25),
        )
        assert tier == "l"
        assert LADDER.cap(tier, "l") == "l"
        assert LADDER.cap(tier, "m") == "m"

    def test_capping_does_not_trim_the_ladder(self) -> None:
        """A band naming xl stays valid; only the decision is capped."""
        tier = LADDER.tier_for("two_axes", {"width": 5_000})
        assert tier == "xl"
        assert LADDER.cap(tier, "l") == "l"
        assert LADDER.order == ("s", "m", "l", "xl")

    def test_the_ceiling_wins_over_a_floor(self) -> None:
        """pinned's floor is m; an explicit ceiling of s is the caller's limit."""
        assert LADDER.cap(LADDER.tier_for("pinned"), "s") == "s"

    def test_an_undeclared_tier_or_ceiling_raises(self) -> None:
        """A ceiling usually comes from config, where a typo must not pass."""
        with pytest.raises(ValueError, match="not a tier of this ladder"):
            LADDER.cap("l", "large")
        with pytest.raises(ValueError, match="not a tier of this ladder"):
            LADDER.cap("large", "l")


def test_the_module_imports_nothing_a_workflow_sandbox_forbids() -> None:
    """ladder.py must stay callable from workflow code.

    The Temporal sandbox forbids I/O, the environment and non-deterministic
    sources. Keeping this module import-light is what lets a workflow ask for a
    tier inline, so the allowed set is asserted rather than described — a new
    import here is a deliberate decision, not an accident.
    """
    import ast
    import pathlib

    import application_sdk.tiering.ladder as mod

    allowed = {"__future__", "collections.abc", "dataclasses", "math"}
    tree = ast.parse(pathlib.Path(mod.__file__).read_text())
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert (
        imported <= allowed
    ), f"ladder.py gained imports: {sorted(imported - allowed)}"


class TestConstructionIsValidated:
    """Every one of these is silent at runtime, which is why it raises here."""

    def test_bands_must_ascend(self) -> None:
        with pytest.raises(ValueError, match="must ascend"):
            Axis(name="rows", bands=(Band(100, "m"), Band(10, "s")))

    def test_bands_must_not_repeat_a_bound(self) -> None:
        """A duplicated bound makes the second band unreachable."""
        with pytest.raises(ValueError, match="must ascend"):
            Axis(name="rows", bands=(Band(10, "s"), Band(10, "m")))

    def test_an_axis_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="name must not be empty"):
            Axis(name="", bands=(Band(INF, "s"),))

    def test_a_band_needs_a_tier(self) -> None:
        with pytest.raises(ValueError, match="band with no tier"):
            Axis(name="rows", bands=(Band(INF, ""),))

    def test_a_sizer_must_say_why(self) -> None:
        """The floor is a measurement; the next person needs to know which."""
        with pytest.raises(ValueError, match="must say why"):
            ActivitySizer(activity="x", floor="s", why="")

    def test_a_sizer_needs_an_activity_and_a_floor(self) -> None:
        with pytest.raises(ValueError, match="activity must not be empty"):
            ActivitySizer(activity="", floor="s", why="measured")
        with pytest.raises(ValueError, match="no floor tier"):
            ActivitySizer(activity="x", floor="", why="measured")

    def test_a_sizer_cannot_declare_one_axis_twice(self) -> None:
        with pytest.raises(ValueError, match="more than once"):
            ActivitySizer(activity="x", floor="s", why="measured", axes=(ROWS, ROWS))

    def test_a_ladder_needs_tiers(self) -> None:
        with pytest.raises(ValueError, match="at least one tier"):
            Ladder(order=(), memory_gib={}, sizers={})

    def test_a_ladder_cannot_repeat_a_tier(self) -> None:
        with pytest.raises(ValueError, match="repeats a tier"):
            Ladder(order=("s", "s"), memory_gib={"s": 6.0}, sizers={})

    def test_every_tier_declares_its_memory(self) -> None:
        """next_up orders by declared memory; an absent tier reads as 0 GiB."""
        with pytest.raises(ValueError, match="missing \\['m'\\]"):
            Ladder(order=("s", "m"), memory_gib={"s": 6.0}, sizers={})

    def test_equal_neighbouring_memory_is_allowed(self) -> None:
        """Non-decreasing, not strictly ascending: tiers may differ only in CPU."""
        assert EQUAL_NEIGHBOURS.order == ("xxs", "xs", "s", "m")
        assert EQUAL_NEIGHBOURS.next_up("xxs") == "m"
        assert list(EQUAL_NEIGHBOURS.climb("xxs")) == ["xxs", "m"]

    def test_memory_must_not_decrease_along_the_order(self) -> None:
        with pytest.raises(ValueError, match="decreases at 'm'"):
            Ladder(order=("s", "m"), memory_gib={"s": 8.0, "m": 0.8}, sizers={})

    @pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), "8", True])
    def test_memory_must_be_a_positive_finite_number(self, bad: object) -> None:
        """Config-built ladders get raw JSON; ValueError is what callers catch."""
        with pytest.raises(ValueError, match="positive, finite"):
            Ladder(order=("s",), memory_gib={"s": bad}, sizers={})

    def test_an_explicit_fallback_tier_must_be_a_real_rung(self) -> None:
        with pytest.raises(ValueError, match="unknown_activity_tier"):
            Ladder(
                order=("a", "b"),
                memory_gib={"a": 1.0, "b": 2.0},
                sizers={},
                unknown_activity_tier="zzz",
            )

    def test_the_fallback_defaults_to_this_ladder_s_own_smallest_rung(self) -> None:
        """Not a hardcoded "s" — an app is free to name its tiers anything."""
        ladder = Ladder(
            order=("tiny", "huge"), memory_gib={"tiny": 1.0, "huge": 9.0}, sizers={}
        )
        assert ladder.unknown_activity_tier == "tiny"
        assert ladder.tier_for("never-registered") == "tiny"

    def test_a_sizer_key_must_match_its_activity(self) -> None:
        """Lookup goes by key, so a mismatch drifts the moment one is renamed."""
        with pytest.raises(ValueError, match="does not match"):
            Ladder(
                order=("s",),
                memory_gib={"s": 6.0},
                sizers={
                    "transform": ActivitySizer(
                        activity="transfrm", floor="s", why="measured"
                    )
                },
            )

    def test_a_floor_outside_the_ladder_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="floor 'huge'"):
            Ladder(
                order=("s",),
                memory_gib={"s": 6.0},
                sizers={"x": ActivitySizer(activity="x", floor="huge", why="measured")},
            )

    def test_an_axis_naming_an_undeclared_tier_is_rejected(self) -> None:
        """Otherwise the axis is ignored and the activity quietly stays on its floor."""
        rogue = Axis(name="rows", bands=(Band(INF, "enormous"),))
        with pytest.raises(ValueError, match="names \\['enormous'\\]"):
            Ladder(
                order=("s",),
                memory_gib={"s": 6.0},
                sizers={
                    "x": ActivitySizer(
                        activity="x", floor="s", why="measured", axes=(rogue,)
                    )
                },
            )

    def test_the_reference_ladder_in_this_module_is_valid(self) -> None:
        """Guards the fixtures above from drifting out of validity themselves."""
        assert LADDER.declared_tiers()

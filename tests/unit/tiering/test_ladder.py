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

    def test_an_axis_with_no_bands_answers_nothing(self) -> None:
        assert Axis(name="empty", bands=()).tier_for(5) is None


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

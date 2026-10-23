"""Resolution is where fail-open lives: no map means every activity stays put."""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.tiering import queues as queues_module
from application_sdk.tiering.queues import (
    SIZE_QUEUE_MAP_ENV,
    queue_map_from_env,
    resolve,
    tier_of_queue,
)

QUEUES = {"s": "app-s", "m": "app-m", "l": "app-l"}


class TestQueueMapFromEnv:
    def test_a_well_formed_map_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, '{"s":"app-s","l":"app-l"}')
        assert queue_map_from_env() == {"s": "app-s", "l": "app-l"}

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "not json at all",
            "[1, 2, 3]",
            '"a string"',
            "null",
            "17",
        ],
    )
    def test_anything_unusable_routes_nothing(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo in a deployment variable must not fail a run that worked yesterday."""
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, raw)
        assert queue_map_from_env() == {}

    def test_unset_routes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SIZE_QUEUE_MAP_ENV, raising=False)
        assert queue_map_from_env() == {}

    def test_blank_keys_and_values_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, '{"s":"app-s","":"app-x","m":""}')
        assert queue_map_from_env() == {"s": "app-s"}

    @pytest.mark.parametrize("value", ["1", '["a"]', "true", "null", "{}"])
    def test_a_non_string_queue_is_dropped_not_coerced(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A list value must not become the queue "['a']".

        A queue no worker polls is worse than no routing: Temporal accepts the
        activity and it sits unscheduled until its start-to-close timeout, with
        every health check green. Dropping leaves the tier unrouted instead.
        """
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, '{"s": %s, "m": "app-m"}' % value)
        assert queue_map_from_env() == {"m": "app-m"}

    def test_a_whitespace_only_queue_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, '{"s": "   ", "m": "app-m"}')
        assert queue_map_from_env() == {"m": "app-m"}

    def test_dropped_entries_are_named_in_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently ignoring half a map is how a rollout looks fine and is not."""
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, '{"s": 1, "m": "app-m"}')
        with mock.patch.object(queues_module, "logger") as log:
            assert queue_map_from_env() == {"m": "app-m"}
        log.warning.assert_called_once()
        assert "'s'" in str(log.warning.call_args)

    def test_a_malformed_map_warns_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, "{not json")
        with mock.patch.object(queues_module, "logger") as log:
            assert queue_map_from_env() == {}
        log.warning.assert_called_once()

    def test_a_clean_map_says_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No warning on the happy path — this runs once per run, per app."""
        monkeypatch.setenv(SIZE_QUEUE_MAP_ENV, '{"s": "app-s"}')
        with mock.patch.object(queues_module, "logger") as log:
            queue_map_from_env()
        log.warning.assert_not_called()

    def test_one_bad_entry_does_not_discard_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            SIZE_QUEUE_MAP_ENV, '{"s": "app-s", "m": null, "l": "app-l"}'
        )
        assert queue_map_from_env() == {"s": "app-s", "l": "app-l"}

    def test_the_variable_name_is_the_one_the_rerouter_reads(self) -> None:
        """Shared contract: the watcher reads this same name off the pod template.

        Renaming it here silently splits the app's ladder from the escalation
        ladder, which only shows up when something is OOM-killed.
        """
        assert SIZE_QUEUE_MAP_ENV == "ATLAN_APP_SIZE_QUEUE_MAP"

    def test_a_custom_variable_can_be_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTHER_MAP", '{"s":"other-s"}')
        assert queue_map_from_env("OTHER_MAP") == {"s": "other-s"}


class TestResolve:
    def test_a_mapped_tier_resolves(self) -> None:
        assert resolve("m", QUEUES) == "app-m"

    def test_an_empty_map_leaves_every_activity_where_it_is(self) -> None:
        """The inert build: a ladder ships, routes nothing, and is the rollback."""
        assert resolve("m", {}) is None

    def test_an_unmapped_tier_leaves_the_activity_where_it_is(self) -> None:
        assert resolve("xl", QUEUES) is None

    @pytest.mark.parametrize("tier", [None, ""])
    def test_no_tier_means_no_queue(self, tier: str | None) -> None:
        assert resolve(tier, QUEUES) is None


class TestTierOfQueue:
    def test_it_inverts_resolve(self) -> None:
        for tier, queue in QUEUES.items():
            assert tier_of_queue(queue, QUEUES) == tier

    def test_the_default_worker_is_not_a_tier(self) -> None:
        """Same rule the rerouter applies: only declared tiers are escalated from."""
        assert tier_of_queue("app-default", QUEUES) is None

    @pytest.mark.parametrize("queue", [None, ""])
    def test_no_queue_means_no_tier(self, queue: str | None) -> None:
        assert tier_of_queue(queue, QUEUES) is None

    def test_an_empty_map_reports_no_tier(self) -> None:
        """While routing is off every attempt reads as untiered, not as 's'."""
        assert tier_of_queue("app-s", {}) is None


class TestTwoTiersOnOneQueue:
    """Legal but a smell — pinned so the behaviour is a decision, not an accident."""

    def test_resolve_sends_both_tiers_to_that_queue(self) -> None:
        shared = {"s": "app-small", "m": "app-small"}
        assert resolve("s", shared) == resolve("m", shared) == "app-small"

    def test_the_inverse_answers_the_first_match_in_declaration_order(self) -> None:
        shared = {"s": "app-small", "m": "app-small"}
        assert tier_of_queue("app-small", shared) == "s"

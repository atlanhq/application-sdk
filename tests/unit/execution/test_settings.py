"""Unit tests for execution settings."""

from __future__ import annotations

import pytest
from temporalio.common import VersioningBehavior

from application_sdk.constants import derive_shutdown_budget, load_shutdown_budget
from application_sdk.execution import settings as settings_mod
from application_sdk.execution.settings import (
    ExecutionSettings,
    InterceptorSettings,
    load_execution_settings,
    load_interceptor_settings,
)

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


class TestExecutionSettingsDefaults:
    """Tests for ExecutionSettings default values."""

    def test_default_host(self) -> None:
        settings = ExecutionSettings()
        assert settings.host == "localhost:7233"

    def test_default_namespace(self) -> None:
        settings = ExecutionSettings()
        assert settings.namespace == "default"

    def test_default_task_queue(self) -> None:
        settings = ExecutionSettings()
        assert settings.task_queue == "application-sdk"

    def test_default_max_concurrent_activities(self) -> None:
        settings = ExecutionSettings()
        assert settings.max_concurrent_activities == 100

    def test_default_graceful_shutdown_timeout(self) -> None:
        settings = ExecutionSettings()
        assert settings.graceful_shutdown_timeout_seconds == 3600

    def test_default_versioning_behavior_is_pinned(self) -> None:
        settings = ExecutionSettings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED

    def test_is_frozen(self) -> None:
        settings = ExecutionSettings()
        with pytest.raises(Exception):
            settings.host = "other:7233"  # type: ignore[misc]


class TestLoadExecutionSettingsFromEnv:
    """Tests for load_execution_settings() reading from environment."""

    def test_host_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_HOST", "temporal.example.com:7233")
        settings = load_execution_settings()
        assert settings.host == "temporal.example.com:7233"

    def test_namespace_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_NAMESPACE", "production")
        settings = load_execution_settings()
        assert settings.namespace == "production"

    def test_task_queue_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "my-custom-queue")
        settings = load_execution_settings()
        assert settings.task_queue == "my-custom-queue"

    def test_max_concurrent_activities_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "50")
        settings = load_execution_settings()
        assert settings.max_concurrent_activities == 50

    @pytest.mark.parametrize("unset", [None, "", "  "])
    def test_max_concurrent_activities_loader_never_reads_the_container(
        self, monkeypatch: pytest.MonkeyPatch, unset: str | None
    ) -> None:
        """Unset or blank, the loader returns 100 even on a tiny pod.

        Derivation happens once in ``create_worker``; every other caller of the
        loader keeps the old contract and does no cgroup reads.
        """
        if unset is None:
            monkeypatch.delenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", raising=False)
        else:
            monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", unset)
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", "512Mi")
        assert load_execution_settings().max_concurrent_activities == 100

    def test_graceful_shutdown_timeout_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "7200")
        settings = load_execution_settings()
        assert settings.graceful_shutdown_timeout_seconds == 7200

    @pytest.mark.parametrize("raw", ["", "abc", "-5"])
    def test_bad_graceful_shutdown_timeout_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """An empty Helm value or a typo must not stop the worker starting."""
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", raw)
        monkeypatch.delenv("ATLAN_SHUTDOWN_GRACE_SECONDS", raising=False)
        assert load_execution_settings().graceful_shutdown_timeout_seconds == 3600

    @pytest.mark.parametrize("raw", ["abc", "0", "-1"])
    def test_bad_max_concurrent_activities_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", raw)
        assert load_execution_settings().max_concurrent_activities == 100

    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in [
            "TEMPORAL_HOST",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_TASK_QUEUE",
            "TEMPORAL_MAX_CONCURRENT_ACTIVITIES",
            "TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT",
        ]:
            monkeypatch.delenv(key, raising=False)
        settings = load_execution_settings()
        assert settings.host == "localhost:7233"
        assert settings.namespace == "default"

    def test_versioning_behavior_auto_upgrade_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "AUTO_UPGRADE")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE

    def test_versioning_behavior_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "auto_upgrade")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE

    def test_versioning_behavior_pinned_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "PINNED")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED

    def test_versioning_behavior_unknown_falls_back_to_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "bogus")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED

    def test_versioning_behavior_unset_defaults_to_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", raising=False)
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED


class TestInterceptorSettingsDefaults:
    """Tests for InterceptorSettings default values."""

    def test_event_interceptor_enabled_by_default(self) -> None:
        settings = InterceptorSettings()
        assert settings.enable_event_interceptor is True

    def test_cleanup_interceptor_disabled_by_default(self) -> None:
        # CleanupInterceptor is deprecated in v3; cleanup is via App.on_complete()
        settings = InterceptorSettings()
        assert settings.enable_cleanup_interceptor is False


class TestLoadInterceptorSettingsFromEnv:
    """Tests for load_interceptor_settings() reading from environment."""

    def test_disable_event_interceptor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "false")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

    def test_disable_cleanup_interceptor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")
        settings = load_interceptor_settings()
        assert settings.enable_cleanup_interceptor is False

    def test_enable_via_true_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "true")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True

    def test_enable_via_yes_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "yes")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True

    def test_enable_via_one_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "1")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True

    def test_disable_via_zero_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "0")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

    def test_disable_via_no_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "no")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

    def test_empty_string_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "")
        settings = load_interceptor_settings()
        # Empty string → default = True
        assert settings.enable_event_interceptor is True

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", raising=False)
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True


class TestShutdownBudget:
    """One grace period drives every shutdown bound (FND-4645)."""

    def test_short_grace_fits_every_step_inside_it(self) -> None:
        """A 30 s grace (SDR installs) must not keep the 1 h drain or 120 s bound."""
        b = derive_shutdown_budget(30, drain_delay_seconds=5)
        assert (b.drain_seconds, b.post_drain_seconds, b.headroom_seconds) == (
            15,
            8,
            2,
        )
        assert 5 + b.drain_seconds + b.post_drain_seconds + b.headroom_seconds <= 30
        # The Pushgateway's stop (hold + two calls) fits in its share of post-drain.
        pushgateway_stop = (
            b.pushgateway_shutdown_delete_delay_seconds
            + 2 * b.pushgateway_http_timeout_seconds
        )
        assert pushgateway_stop <= b.post_drain_seconds / 2

    def test_twelve_hour_grace_keeps_defaults_and_drains_for_it(self) -> None:
        """A 12 h grace must not cancel long activities after 1 h."""
        b = derive_shutdown_budget(43230, drain_delay_seconds=5)
        assert b.post_drain_seconds == 120
        assert b.headroom_seconds == 30
        assert b.pushgateway_http_timeout_seconds == 10
        assert b.pushgateway_shutdown_delete_delay_seconds == 35
        assert b.drain_seconds == 43230 - 5 - 120 - 30

    def test_grace_just_above_the_default_drain_keeps_the_full_drain(self) -> None:
        """The deleted SDK chart paired a 3660 s grace with the 3600 s drain.
        Declaring that grace must not shorten the drain: the steps after it are
        best-effort and shrink into the 55 s left instead."""
        b = derive_shutdown_budget(3660, drain_delay_seconds=5)
        assert b.drain_seconds == 3600
        assert 5 + b.drain_seconds + b.post_drain_seconds + b.headroom_seconds == 3660
        assert b.pushgateway_shutdown_delete_delay_seconds < 35

    def test_tail_never_drops_below_its_floor_to_feed_the_drain(self) -> None:
        """A 600 s grace cannot hold the 3600 s drain, so it drains for all but
        the 10 s floor after it."""
        b = derive_shutdown_budget(600, drain_delay_seconds=5)
        assert b.drain_seconds == 585
        assert b.post_drain_seconds + b.headroom_seconds == 10

    def test_explicit_drain_sets_what_the_tail_gets(self) -> None:
        """With an explicit 600 s drain, a 3660 s grace has room for the full
        default tail, so it is not squeezed."""
        b = derive_shutdown_budget(
            3660, drain_delay_seconds=5, preferred_drain_seconds=600
        )
        assert (b.post_drain_seconds, b.headroom_seconds) == (120, 30)

    @pytest.mark.parametrize("grace", [1, 4, 30, 60, 600, 3600, 43230, 86400])
    def test_bounds_never_exceed_grace(self, grace: int) -> None:
        b = derive_shutdown_budget(grace, drain_delay_seconds=0)
        assert b.post_drain_seconds >= 1  # 0 would disable the deadline
        assert b.drain_seconds >= 0
        assert b.pushgateway_shutdown_delete_delay_seconds >= 0
        assert b.drain_seconds + b.post_drain_seconds + b.headroom_seconds <= grace
        if grace >= 6:
            b5 = derive_shutdown_budget(grace, drain_delay_seconds=5)
            assert (
                5 + b5.drain_seconds + b5.post_drain_seconds + b5.headroom_seconds
                <= grace
            )
            # Drain first: never shorter than the default whenever the grace
            # can hold it alongside the 10 s tail floor.
            if grace >= 5 + 3600 + 10:
                assert b5.drain_seconds >= 3600

    def test_grace_too_short_for_the_drain_delay_is_rejected(self) -> None:
        """A 1 s grace cannot even cover the 5 s delay before Temporal shutdown."""
        with pytest.raises(ValueError, match="at least 6s"):
            derive_shutdown_budget(1, drain_delay_seconds=5)
        assert derive_shutdown_budget(6, drain_delay_seconds=5).drain_seconds == 0

    def test_grace_too_short_warns_and_keeps_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "1")
        with pytest.warns(UserWarning, match="ATLAN_SHUTDOWN_DRAIN_DELAY_SECONDS"):
            assert load_shutdown_budget() is None

    def test_unset_grace_means_no_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_SHUTDOWN_GRACE_SECONDS", raising=False)
        assert load_shutdown_budget() is None

    @pytest.mark.parametrize("raw", ["", "0", "-5"])
    def test_non_positive_grace_means_no_budget(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", raw)
        assert load_shutdown_budget() is None

    def test_non_integer_grace_warns_and_means_no_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30s")
        with pytest.warns(UserWarning, match="ATLAN_SHUTDOWN_GRACE_SECONDS"):
            assert load_shutdown_budget() is None

    def test_drain_derives_from_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "43230")
        expected = derive_shutdown_budget(43230).drain_seconds
        assert load_execution_settings().graceful_shutdown_timeout_seconds == expected
        assert expected > 3600

    def test_explicit_drain_feeds_the_derived_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "600")
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "3660")
        budget = load_shutdown_budget()
        assert budget is not None and budget.post_drain_seconds == 120

    def test_explicit_drain_beats_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "7200")
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        assert load_execution_settings().graceful_shutdown_timeout_seconds == 7200

    def test_drain_default_unchanged_without_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
        monkeypatch.delenv("ATLAN_SHUTDOWN_GRACE_SECONDS", raising=False)
        assert load_execution_settings().graceful_shutdown_timeout_seconds == 3600


class TestDeriveMaxConcurrentActivities:
    """``min(100, 32 x cpus, memory // 64 MiB)``, floored at 16 (FND-4646)."""

    @pytest.mark.parametrize(
        ("cpus", "memory", "expected"),
        [
            # No known memory limit: CPU alone, capped at the old default.
            (1, None, 32),
            (2, None, 64),
            (4, None, 100),
            (64, None, 100),
            # Memory is the tighter bound.
            (4, 2 * _GIB, 32),
            (8, 3 * _GIB, 48),
            # CPU is the tighter bound.
            (2, 16 * _GIB, 64),
            # Both generous: the cap.
            (32, 64 * _GIB, 100),
            # The fleet's typical half-CPU, sub-GiB pod lands on the floor.
            (1, 700 * _MIB, 16),
            (1, 0, 16),
        ],
    )
    def test_formula(self, cpus: int, memory: int | None, expected: int) -> None:
        assert settings_mod._derive_max_concurrent_activities(cpus, memory) == expected

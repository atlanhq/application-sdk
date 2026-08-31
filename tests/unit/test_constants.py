"""Unit tests for env-var loaders in :mod:`application_sdk.constants`."""

import importlib

import pytest

import application_sdk.constants as constants
from application_sdk.constants import _load_worker_liveness_max_idle_seconds


class TestLoadWorkerLivenessMaxIdleSeconds:
    """Cover the ``ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS`` loader."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS", raising=False)
        assert _load_worker_liveness_max_idle_seconds() == 0.0

    def test_valid_positive_value(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS", "30")
        assert _load_worker_liveness_max_idle_seconds() == 30.0

    def test_negative_clamped_to_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS", "-5")
        assert _load_worker_liveness_max_idle_seconds() == 0.0

    def test_non_numeric_falls_back_to_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS", "abc")
        with pytest.warns(UserWarning, match="not a valid number"):
            assert _load_worker_liveness_max_idle_seconds() == 0.0

    def test_inf_falls_back_to_zero(self, monkeypatch: pytest.MonkeyPatch):
        # ``inf`` parses but a window that can never trip is silently useless.
        monkeypatch.setenv("ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS", "inf")
        with pytest.warns(UserWarning, match="not finite"):
            assert _load_worker_liveness_max_idle_seconds() == 0.0

    def test_nan_falls_back_to_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS", "nan")
        with pytest.warns(UserWarning, match="not finite"):
            assert _load_worker_liveness_max_idle_seconds() == 0.0


class TestUseServerSideCursor:
    """Cover the ``ATLAN_SQL_USE_SERVER_SIDE_CURSOR`` opt-out.

    The flag is a module-level constant evaluated at import time, so each case
    reloads :mod:`application_sdk.constants` under a patched environment.
    """

    @pytest.fixture(autouse=True)
    def _restore_constants(self):
        # Leave the module as the rest of the suite expects to find it.
        yield
        importlib.reload(constants)

    def _reload(self) -> bool:
        return importlib.reload(constants).USE_SERVER_SIDE_CURSOR

    def test_default_enabled_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", raising=False)
        assert self._reload() is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", " false ", "0", "no"])
    def test_opt_out_disables(self, monkeypatch: pytest.MonkeyPatch, value: str):
        # Regression: ``bool(os.getenv(...))`` made every non-empty string —
        # including "false" — truthy, so the documented opt-out could not
        # disable server-side cursors.
        monkeypatch.setenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", value)
        assert self._reload() is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", " true "])
    def test_explicit_true_enables(self, monkeypatch: pytest.MonkeyPatch, value: str):
        monkeypatch.setenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", value)
        assert self._reload() is True

    def test_empty_string_still_disables(self, monkeypatch: pytest.MonkeyPatch):
        # The only opt-out that worked before the fix must keep working.
        monkeypatch.setenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "")
        assert self._reload() is False

    def test_sql_client_default_follows_the_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """BaseSQLClient binds the constant as its default at import time."""
        monkeypatch.setenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "false")
        importlib.reload(constants)

        import application_sdk.clients.sql as sql_client_mod

        try:
            reloaded = importlib.reload(sql_client_mod)
            assert reloaded.BaseSQLClient.use_server_side_cursor is False
        finally:
            importlib.reload(constants)
            importlib.reload(sql_client_mod)


class TestStorageLockWaitProgressSeconds:
    """Cover the ``ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS`` clamp.

    The value is load-bearing config, not a cosmetic interval: a waiter queued
    behind another activity's multi-GB download makes no transfer progress of
    its own, so it marks progress on this interval to avoid being killed by the
    stall watchdog. The interval must therefore stay under the watchdog budget
    an operator configured — which is why the default is capped at a quarter of
    ``ATLAN_MAX_NO_PROGRESS_SECONDS`` rather than a bare 30. Evaluated at import
    time, so each case reloads the module under a patched environment.
    (CONNECT-1126)
    """

    @pytest.fixture(autouse=True)
    def _restore_constants(self):
        yield
        importlib.reload(constants)

    def _reload(self, monkeypatch: pytest.MonkeyPatch, **env: str | None) -> float:
        for name in (
            "ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS",
            "ATLAN_MAX_NO_PROGRESS_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            if value is not None:
                monkeypatch.setenv(name, value)
        return importlib.reload(constants).STORAGE_LOCK_WAIT_PROGRESS_SECONDS

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        # 30 sits well under a quarter of the 900s default budget, so the cap
        # is inactive and the documented default survives.
        assert self._reload(monkeypatch) == 30.0

    def test_custom_value_under_the_cap_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        assert (
            self._reload(monkeypatch, ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS="10")
            == 10.0
        )

    def test_lowered_watchdog_budget_pulls_the_interval_down(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The invariant this constant exists for: an operator who lowers the
        # budget below the interval would otherwise get the exact failure the
        # marking prevents — a correctly-waiting activity killed as stalled.
        assert self._reload(monkeypatch, ATLAN_MAX_NO_PROGRESS_SECONDS="20") == 5.0

    def test_explicit_value_above_the_cap_is_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        assert (
            self._reload(
                monkeypatch,
                ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS="600",
                ATLAN_MAX_NO_PROGRESS_SECONDS="120",
            )
            == 30.0
        )

    @pytest.mark.parametrize("value", ["0", "-5", "0.25"])
    def test_sub_second_values_are_floored(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        # A zero or negative timeout on the guard's ``asyncio.wait_for`` would
        # turn the queued wait into a busy spin.
        assert (
            self._reload(monkeypatch, ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS=value)
            == 1.0
        )

    def test_tiny_watchdog_budget_still_floors_at_one_second(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The floor wins over the cap: better a waiter that marks too often
        # than one that spins.
        assert self._reload(monkeypatch, ATLAN_MAX_NO_PROGRESS_SECONDS="2") == 1.0

    async def test_a_contended_guard_marks_progress_at_the_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The clamp is only worth anything if the guard actually uses it.

        Pins the wiring end to end: a second caller blocked on a held lock
        marks progress once per interval rather than waiting silently.
        """
        import asyncio
        from unittest.mock import MagicMock, patch

        from application_sdk.storage._locks import PathLockRegistry

        registry = PathLockRegistry("test.lock_wait")
        tracker = MagicMock()

        with (
            patch.object(constants, "STORAGE_LOCK_WAIT_PROGRESS_SECONDS", 0.01),
            patch(
                "application_sdk.storage._locks.current_progress_tracker",
                return_value=tracker,
            ),
        ):
            async with registry.guard("/tmp/contended"):
                waiter = asyncio.create_task(
                    self._hold_briefly(registry, "/tmp/contended")
                )
                await asyncio.sleep(0.1)
                assert tracker.mark_progress.call_count >= 2, (
                    "a queued waiter marked progress "
                    f"{tracker.mark_progress.call_count} times in ~10 intervals — "
                    "the stall watchdog would kill it"
                )
                assert tracker.mark_progress.call_args[0][0] == "test.lock_wait"
            await waiter

    @staticmethod
    async def _hold_briefly(registry, path: str) -> None:
        async with registry.guard(path):
            pass

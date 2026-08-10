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

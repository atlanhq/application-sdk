"""Unit tests for env-var loaders in :mod:`application_sdk.constants`."""

import pytest

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

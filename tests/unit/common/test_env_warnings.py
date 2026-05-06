"""Unit tests for the removed-env-var startup warning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from application_sdk.common.env_warnings import _REMOVED_ENV_VARS, warn_removed_env_vars


class TestWarnRemovedEnvVars:
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch):
        for name in _REMOVED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        yield

    def _patch_logger(self):
        """Patch the lazy ``get_logger`` import inside ``warn_removed_env_vars``."""
        mock_logger = MagicMock()
        return (
            patch(
                "application_sdk.observability.logger_adaptor.get_logger",
                return_value=mock_logger,
            ),
            mock_logger,
        )

    def _formatted_message(self, mock_logger: MagicMock) -> str:
        """Render the single warning the helper emits, % args interpolated."""
        mock_logger.warning.assert_called_once()
        args, _ = mock_logger.warning.call_args
        return args[0] % args[1:]

    def test_silent_when_no_removed_vars_set(self) -> None:
        ctx, mock_logger = self._patch_logger()
        with ctx:
            warn_removed_env_vars()
        mock_logger.warning.assert_not_called()

    def test_single_warning_lists_one_set_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "true")
        ctx, mock_logger = self._patch_logger()
        with ctx:
            warn_removed_env_vars()

        msg = self._formatted_message(mock_logger)
        assert "ATLAN_ENABLE_APP_VITALS" in msg

    def test_single_warning_lists_all_set_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "true")
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_METRICS", "http://x")
        monkeypatch.setenv("ATLAN_TRACES_BATCH_SIZE", "100")
        ctx, mock_logger = self._patch_logger()
        with ctx:
            warn_removed_env_vars()

        msg = self._formatted_message(mock_logger)
        assert "ATLAN_ENABLE_APP_VITALS" in msg
        assert "ATLAN_ENABLE_OTLP_METRICS" in msg
        assert "ATLAN_TRACES_BATCH_SIZE" in msg

    def test_warning_lists_vars_alphabetically_for_stable_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_METRICS", "http://x")
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "true")
        ctx, mock_logger = self._patch_logger()
        with ctx:
            warn_removed_env_vars()

        msg = self._formatted_message(mock_logger)
        # Sorted: ATLAN_ENABLE_APP_VITALS comes before ATLAN_ENABLE_OTLP_METRICS.
        assert msg.index("ATLAN_ENABLE_APP_VITALS") < msg.index(
            "ATLAN_ENABLE_OTLP_METRICS"
        )

    def test_empty_string_value_does_not_trigger_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``os.environ.get`` returns "" for an explicitly-set empty value, which
        # is falsy and should be treated as "not set".
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "")
        ctx, mock_logger = self._patch_logger()
        with ctx:
            warn_removed_env_vars()
        mock_logger.warning.assert_not_called()

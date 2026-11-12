"""Unit tests for the shared env-var readers (FND-4891)."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from application_sdk import _env
from application_sdk.common import _env as common_env

KEY = "ATLAN_TEST_ENV_HELPER"


@pytest.fixture(autouse=True)
def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY, raising=False)


class TestEnvInt:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_unset_or_empty_is_the_default_without_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        if raw is not None:
            monkeypatch.setenv(KEY, raw)
        reports: list[str] = []
        assert _env.env_int(KEY, 7, report=reports.append) == 7
        assert reports == []

    def test_valid_value_with_padding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY, " 12\n")
        assert _env.env_int(KEY, 7) == 12

    @pytest.mark.parametrize("raw", ["abc", "1.5", "inf"])
    def test_malformed_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(KEY, raw)
        with pytest.warns(RuntimeWarning, match=f"{KEY}=.*not an integer.*7"):
            assert _env.env_int(KEY, 7) == 7

    def test_below_minimum_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, "0")
        with pytest.warns(RuntimeWarning, match="below 1"):
            assert _env.env_int(KEY, 300, minimum=1) == 300

    def test_minimum_itself_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY, "1")
        assert _env.env_int(KEY, 300, minimum=1) == 1


class TestEnvFloat:
    def test_valid_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY, "2.5")
        assert _env.env_float(KEY, 1.0) == 2.5

    def test_empty_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY, "")
        assert _env.env_float(KEY, 1.0) == 1.0

    def test_malformed_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, "ten")
        with pytest.warns(RuntimeWarning, match="not a number"):
            assert _env.env_float(KEY, 1.0) == 1.0

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
    def test_non_finite_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(KEY, raw)
        with pytest.warns(RuntimeWarning, match="not finite"):
            assert _env.env_float(KEY, 1.0) == 1.0

    def test_below_minimum_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, "-1")
        with pytest.warns(RuntimeWarning, match="below 0"):
            assert _env.env_float(KEY, 30.0, minimum=0.0) == 30.0


class TestEnvBool:
    @pytest.mark.parametrize("raw", ["true", "TRUE", " 1 ", "yes", "Yes", "on"])
    def test_true_words(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(KEY, raw)
        assert _env.env_bool(KEY, False) is True

    @pytest.mark.parametrize("raw", ["false", "False", " 0 ", "no", "NO", "off"])
    def test_false_words(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(KEY, raw)
        assert _env.env_bool(KEY, True) is False

    @pytest.mark.parametrize("default", [True, False])
    def test_empty_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, default: bool
    ) -> None:
        monkeypatch.setenv(KEY, "")
        assert _env.env_bool(KEY, default) is default

    @pytest.mark.parametrize("default", [True, False])
    def test_unknown_word_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, default: bool
    ) -> None:
        monkeypatch.setenv(KEY, "enabled")
        with pytest.warns(RuntimeWarning, match=f"{KEY}='enabled' is not one of"):
            assert _env.env_bool(KEY, default) is default

    def test_vocabularies_are_disjoint(self) -> None:
        assert not (_env.TRUE_VALUES & _env.FALSE_VALUES)


class TestEnvChoice:
    def test_value_is_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY, " API\n")
        assert _env.env_choice(KEY, ("api", "none"), "none") == "api"

    def test_unset_is_the_default(self) -> None:
        assert _env.env_choice(KEY, ("api", "none"), "none") == "none"

    def test_unknown_word_warns_and_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, "apiserver")
        with pytest.warns(RuntimeWarning, match="is not one of"):
            assert _env.env_choice(KEY, ("api", "none"), "none") == "none"


class TestReport:
    def test_report_replaces_the_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY, "abc")
        reports: list[str] = []
        with patch("warnings.warn") as warn:
            assert _env.env_int(KEY, 3, report=reports.append) == 3
        warn.assert_not_called()
        assert len(reports) == 1
        assert KEY in reports[0] and "'abc'" in reports[0]

    def test_common_flavour_logs_through_the_sdk_logger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, "abc")
        with patch.object(common_env.logger, "warning") as warning:
            assert common_env.env_int(KEY, 3) == 3
        warning.assert_called_once()
        assert KEY in str(warning.call_args)


class TestImportTime:
    """The bug: an empty Helm value stopped every process at import."""

    @pytest.mark.parametrize(
        "env",
        [
            {"ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS": ""},
            {"OTEL_BATCH_SIZE": "abc"},
            {"ATLAN_SEGMENT_BATCH_TIMEOUT_SECONDS": "ten"},
            {"ATLAN_DAPR_COLD_START_MAX_WAIT_SECONDS": ""},
        ],
    )
    def test_malformed_value_does_not_stop_import(self, env: dict[str, str]) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import application_sdk.constants, "
                "application_sdk.infrastructure._dapr.http",
            ],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

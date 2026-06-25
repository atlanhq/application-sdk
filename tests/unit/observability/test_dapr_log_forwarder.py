"""Unit tests for the daprd log forwarder."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.observability import dapr_log_forwarder as dlf


class TestSplitChildCommand:
    def test_returns_args_after_separator(self):
        argv = ["dapr_log_forwarder", "--", "daprd", "--app-id", "app"]
        assert dlf._split_child_command(argv) == ["daprd", "--app-id", "app"]

    def test_falls_back_to_all_args_without_separator(self):
        argv = ["dapr_log_forwarder", "daprd", "--app-id", "app"]
        assert dlf._split_child_command(argv) == ["daprd", "--app-id", "app"]

    def test_empty_child_after_separator(self):
        assert dlf._split_child_command(["dapr_log_forwarder", "--"]) == []


class TestFormatLine:
    def test_parses_json_and_folds_scope_into_message(self):
        line = json.dumps(
            {
                "level": "warning",
                "msg": "A non-YAML Subscription file ..data was detected",
                "scope": "dapr.runtime.loader.disk",
                "type": "log",
            }
        )
        level, message = dlf._format_line(line + "\n")
        assert level == "warning"
        assert message == (
            "[dapr.runtime.loader.disk] "
            "A non-YAML Subscription file ..data was detected"
        )

    def test_json_without_scope(self):
        level, message = dlf._format_line(json.dumps({"level": "info", "msg": "up"}))
        assert (level, message) == ("info", "up")

    def test_non_json_line_forwarded_verbatim_at_info(self):
        level, message = dlf._format_line("plain text daprd line\n")
        assert (level, message) == ("info", "plain text daprd line")

    def test_json_array_treated_as_plain(self):
        level, message = dlf._format_line("[1, 2, 3]")
        assert (level, message) == ("info", "[1, 2, 3]")

    def test_missing_level_defaults_to_info(self):
        level, _ = dlf._format_line(json.dumps({"msg": "no level"}))
        assert level == "info"


class TestLevelMapping:
    @pytest.mark.parametrize(
        "dapr_level,method",
        [
            ("debug", "debug"),
            ("info", "info"),
            ("warning", "warning"),
            ("warn", "warning"),
            ("error", "error"),
            ("fatal", "critical"),
            ("panic", "critical"),
        ],
    )
    def test_known_levels(self, dapr_level, method):
        assert dlf._LEVEL_TO_METHOD[dapr_level] == method


class TestMakeEmitter:
    def test_routes_to_logger_method_by_level(self):
        fake_logger = MagicMock()
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=fake_logger,
        ):
            emit = dlf._make_emitter()
            emit("warning", "hello")
        fake_logger.warning.assert_called_once_with("hello")

    def test_unknown_level_falls_back_to_info(self):
        fake_logger = MagicMock()
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=fake_logger,
        ):
            emit = dlf._make_emitter()
            emit("nonsense", "hello")
        fake_logger.info.assert_called_once_with("hello")

    def test_logger_setup_failure_falls_back_to_stderr(self, capsys):
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            side_effect=RuntimeError("boom"),
        ):
            emit = dlf._make_emitter()
            emit("error", "still visible")
        assert "still visible" in capsys.readouterr().err

    def test_emit_failure_falls_back_to_stderr(self, capsys):
        fake_logger = MagicMock()
        fake_logger.warning.side_effect = RuntimeError("sink down")
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=fake_logger,
        ):
            emit = dlf._make_emitter()
            emit("warning", "fallback line")
        assert "fallback line" in capsys.readouterr().err


class TestMain:
    def test_no_child_command_returns_error_code(self):
        assert dlf.main(["dapr_log_forwarder", "--"]) == 2

    def test_transparent_exec_when_flag_disabled(self):
        # os.execvp never returns in reality; simulate that with SystemExit so
        # main() doesn't fall through into the forwarding path.
        with (
            patch("application_sdk.constants.ENABLE_DAPR_LOG_FORWARDING", False),
            patch.object(
                dlf, "_exec_transparently", side_effect=SystemExit(0)
            ) as exec_mock,
        ):
            with pytest.raises(SystemExit):
                dlf.main(["dapr_log_forwarder", "--", "daprd", "--app-id", "app"])
        exec_mock.assert_called_once_with(["daprd", "--app-id", "app"])

    def test_forwards_each_line_and_returns_child_exit_code(self):
        lines = [
            json.dumps({"level": "warning", "msg": "w", "scope": "s"}) + "\n",
            json.dumps({"level": "error", "msg": "e"}) + "\n",
        ]
        fake_proc = MagicMock()
        fake_proc.stdout = iter(lines)
        fake_proc.poll.return_value = None
        fake_proc.wait.return_value = 0

        emitted: list[tuple[str, str]] = []
        with (
            patch("application_sdk.constants.ENABLE_DAPR_LOG_FORWARDING", True),
            patch.object(
                dlf,
                "_make_emitter",
                return_value=lambda lvl, msg: emitted.append((lvl, msg)),
            ),
            patch.object(dlf.subprocess, "Popen", return_value=fake_proc),
            patch.object(dlf.signal, "signal"),
            patch.object(dlf, "_flush_observability"),
        ):
            rc = dlf.main(["dapr_log_forwarder", "--", "daprd"])

        assert rc == 0
        assert emitted == [("warning", "[s] w"), ("error", "e")]

    def test_flushes_observability_on_exit(self):
        fake_proc = MagicMock()
        fake_proc.stdout = iter([])
        fake_proc.wait.return_value = 3

        with (
            patch("application_sdk.constants.ENABLE_DAPR_LOG_FORWARDING", True),
            patch.object(dlf, "_make_emitter", return_value=lambda *a: None),
            patch.object(dlf.subprocess, "Popen", return_value=fake_proc),
            patch.object(dlf.signal, "signal"),
            patch.object(dlf, "_flush_observability") as flush_mock,
        ):
            rc = dlf.main(["dapr_log_forwarder", "--", "daprd"])

        assert rc == 3
        flush_mock.assert_called_once()

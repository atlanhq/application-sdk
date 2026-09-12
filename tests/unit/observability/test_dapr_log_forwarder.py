"""Unit tests for the daprd log forwarder."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
            emit, _complete = dlf._make_emitter()
            emit("warning", "hello")
        fake_logger.warning.assert_called_once_with("hello")

    def test_unknown_level_falls_back_to_info(self):
        fake_logger = MagicMock()
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=fake_logger,
        ):
            emit, _complete = dlf._make_emitter()
            emit("nonsense", "hello")
        fake_logger.info.assert_called_once_with("hello")

    def test_logger_setup_failure_falls_back_to_stderr(self, capsys):
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            side_effect=RuntimeError("boom"),
        ):
            emit, complete = dlf._make_emitter()
            emit("error", "still visible")
        assert complete is None
        assert "still visible" in capsys.readouterr().err

    def test_emit_failure_falls_back_to_stderr(self, capsys):
        fake_logger = MagicMock()
        fake_logger.warning.side_effect = RuntimeError("sink down")
        with patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=fake_logger,
        ):
            emit, _complete = dlf._make_emitter()
            emit("warning", "fallback line")
        assert "fallback line" in capsys.readouterr().err


class _FakeStdout:
    """StreamReader-like stand-in for ``proc.stdout`` used by the readuntil loop."""

    def __init__(self, lines: list[bytes]):
        self._buf = b"".join(lines)

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        idx = self._buf.find(separator)
        if idx == -1:
            partial, self._buf = self._buf, b""
            raise asyncio.IncompleteReadError(partial, None)
        end = idx + len(separator)
        chunk, self._buf = self._buf[:end], self._buf[end:]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            partial, self._buf = self._buf, b""
            raise asyncio.IncompleteReadError(partial, n)
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


class _FakeProc:
    def __init__(self, lines: list[bytes], returncode: int):
        self.stdout = _FakeStdout(lines)
        self.returncode = None
        self._rc = returncode

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc


class TestForwarderLogLevel:
    """The forwarder gates at the more verbose of LOG_LEVEL and DAPR_LOG_LEVEL."""

    def test_dapr_debug_lowers_an_info_app(self, monkeypatch):
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        assert dlf._forwarder_log_level() == "DEBUG"

    def test_image_default_info_never_raises_a_quieter_app_to_error(self, monkeypatch):
        """LOG_LEVEL=ERROR with the image default DAPR_LOG_LEVEL=info: the process
        gates at INFO so daprd info lines pass *as INFO* — nothing is promoted."""
        monkeypatch.setenv("DAPR_LOG_LEVEL", "info")
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        assert dlf._forwarder_log_level() == "INFO"

    def test_atlan_log_level_takes_precedence_over_log_level(self, monkeypatch):
        monkeypatch.setenv("DAPR_LOG_LEVEL", "warn")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.setenv("ATLAN_LOG_LEVEL", "DEBUG")
        assert dlf._forwarder_log_level() == "DEBUG"

    def test_unset_dapr_log_level_uses_the_entrypoint_warn_fallback(self, monkeypatch):
        """With DAPR_LOG_LEVEL unset, entrypoint.sh gates daprd at ``warn``
        (``${DAPR_LOG_LEVEL:-warn}``), so this process must resolve the same
        level — not a second, more verbose default of its own."""
        monkeypatch.delenv("DAPR_LOG_LEVEL", raising=False)
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        assert dlf._forwarder_log_level() == "WARNING"
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        assert dlf._forwarder_log_level() == "INFO"

    def test_unknown_level_names_return_none(self, monkeypatch):
        monkeypatch.setenv("DAPR_LOG_LEVEL", "verbose")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        assert dlf._forwarder_log_level() is None
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
        assert dlf._forwarder_log_level() is None


class TestMain:
    def test_reexecs_once_with_atlan_log_level_when_daprd_more_verbose(
        self, monkeypatch
    ):
        """SDR mode, DAPR_LOG_LEVEL=debug, app at INFO: main() must re-exec itself
        with ATLAN_LOG_LEVEL=DEBUG before running the forwarder, since the SDK
        sinks were already built at INFO by the package import."""
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        argv = ["dapr_log_forwarder", "--", "daprd", "--app-id", "app"]
        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            # The re-exec is POSIX-only (see test_no_reexec_on_windows), so pin
            # the branch rather than letting the runner's platform decide.
            patch.object(dlf.os, "name", "posix"),
            patch.object(dlf.os, "execve", side_effect=SystemExit(0)) as execve,
            patch.object(dlf, "_run") as run_mock,
            pytest.raises(SystemExit),
        ):
            dlf.main(argv)
        run_mock.assert_not_called()
        (exe, cmd, env), _ = execve.call_args
        assert exe == dlf.sys.executable
        assert cmd[:2] == [dlf.sys.executable, "-m"]
        assert cmd[2].endswith("dapr_log_forwarder")
        assert cmd[3:] == ["--", "daprd", "--app-id", "app"]
        assert env["ATLAN_LOG_LEVEL"] == "DEBUG"

    def test_no_reexec_without_a_module_spec(self, monkeypatch):
        """Run as a file path rather than with ``-m``, there is no importable name
        to re-exec: ``python -m __main__`` would exec successfully and then die on
        ``__main__.__spec__ is None``, taking daprd with it. Forward at the
        current level instead."""
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        monkeypatch.setattr(dlf, "__spec__", None)

        async def _fake_run(child_cmd: list[str]) -> int:
            return 0

        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            patch.object(dlf.os, "execve") as execve,
            patch.object(dlf, "_run", _fake_run),
        ):
            assert dlf.main(["dapr_log_forwarder", "--", "daprd"]) == 0
        assert execve.call_count == 0  # not assert_not_called: it repr's the env

    def test_no_reexec_on_windows(self, monkeypatch):
        """Windows has no exec: os.execve spawns a copy and exits this process,
        which would drop daprd's supervisor out from under its caller. Forward at
        the current level instead — the same fallback as a failed exec."""
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)

        async def _fake_run(child_cmd: list[str]) -> int:
            return 0

        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            patch.object(dlf.os, "name", "nt"),
            patch.object(dlf.os, "execve") as execve,
            patch.object(dlf, "_run", _fake_run),
        ):
            assert dlf.main(["dapr_log_forwarder", "--", "daprd"]) == 0
        assert execve.call_count == 0  # not assert_not_called: it repr's the env

    def test_no_reexec_once_level_already_matches(self, monkeypatch):
        """The re-exec'd process (ATLAN_LOG_LEVEL already DEBUG) must fall straight
        through to the forwarder — no exec loop."""
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.setenv("ATLAN_LOG_LEVEL", "DEBUG")

        async def _fake_run(child_cmd: list[str]) -> int:
            return 0

        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            patch.object(dlf.os, "execve") as execve,
            patch.object(dlf, "_run", _fake_run),
        ):
            assert dlf.main(["dapr_log_forwarder", "--", "daprd"]) == 0
        assert execve.call_count == 0  # not assert_not_called: it repr's the env

    def test_no_reexec_at_image_defaults(self, monkeypatch):
        """DAPR_LOG_LEVEL=info (Dockerfile ENV) with LOG_LEVEL=INFO: nothing to do."""
        monkeypatch.setenv("DAPR_LOG_LEVEL", "info")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)

        async def _fake_run(child_cmd: list[str]) -> int:
            return 0

        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            patch.object(dlf.os, "execve") as execve,
            patch.object(dlf, "_run", _fake_run),
        ):
            assert dlf.main(["dapr_log_forwarder", "--", "daprd"]) == 0
        assert execve.call_count == 0  # not assert_not_called: it repr's the env

    def test_exec_failure_falls_back_to_forwarding_at_current_level(self, monkeypatch):
        monkeypatch.setenv("DAPR_LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)

        async def _fake_run(child_cmd: list[str]) -> int:
            return 3

        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            # Without this the case is vacuous off POSIX: no exec is attempted
            # there, so the OSError path would never be reached.
            patch.object(dlf.os, "name", "posix"),
            patch.object(dlf.os, "execve", side_effect=OSError("no exec")),
            patch.object(dlf, "_run", _fake_run),
        ):
            assert dlf.main(["dapr_log_forwarder", "--", "daprd"]) == 3

    def test_no_child_command_returns_error_code(self):
        assert dlf.main(["dapr_log_forwarder", "--"]) == 2

    def test_transparent_exec_when_not_sdr_mode(self):
        # Outside SDR mode (ENABLE_ATLAN_UPLOAD=false) the child is exec'd directly.
        # os.execvp never returns in reality; simulate that with SystemExit so
        # main() doesn't fall through into the forwarding path.
        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", False),
            patch.object(
                dlf, "_exec_transparently", side_effect=SystemExit(0)
            ) as exec_mock,
            pytest.raises(SystemExit),
        ):
            dlf.main(["dapr_log_forwarder", "--", "daprd", "--app-id", "app"])
        exec_mock.assert_called_once_with(["daprd", "--app-id", "app"])

    def test_active_forwarding_path_when_sdr_mode(self, monkeypatch):
        # In SDR mode (ENABLE_ATLAN_UPLOAD=true) main() must NOT exec daprd
        # transparently; it runs the forwarder via asyncio.run(_run(child_cmd))
        # and returns daprd's exit code.
        monkeypatch.setenv("DAPR_LOG_LEVEL", "info")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        captured: dict[str, list[str]] = {}

        async def _fake_run(child_cmd: list[str]) -> int:
            captured["child_cmd"] = child_cmd
            return 7

        with (
            patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            patch.object(dlf, "_exec_transparently") as exec_mock,
            patch.object(dlf, "_run", _fake_run),
        ):
            rc = dlf.main(["dapr_log_forwarder", "--", "daprd", "--app-id", "app"])

        exec_mock.assert_not_called()
        assert captured["child_cmd"] == ["daprd", "--app-id", "app"]
        assert rc == 7


class TestRun:
    def test_forwards_each_line_and_returns_child_exit_code(self):
        lines = [
            (
                json.dumps({"level": "warning", "msg": "w", "scope": "s"}) + "\n"
            ).encode(),
            (json.dumps({"level": "error", "msg": "e"}) + "\n").encode(),
        ]
        fake_proc = _FakeProc(lines, returncode=0)
        emitted: list[tuple[str, str]] = []

        async def _create(*_a, **_k):
            return fake_proc

        with (
            patch.object(
                dlf,
                "_make_emitter",
                return_value=(lambda lvl, msg: emitted.append((lvl, msg)), None),
            ),
            patch.object(dlf.asyncio, "create_subprocess_exec", _create),
            patch.object(dlf, "_install_signal_forwarding"),
            patch.object(dlf, "_drain_and_flush", new=_async_noop),
        ):
            rc = asyncio.run(dlf._run(["daprd"]))

        assert rc == 0
        assert emitted == [("warning", "[s] w"), ("error", "e")]

    def test_oversized_line_emits_truncated_warning_and_continues(self):
        # A line longer than the buffer limit triggers LimitOverrunError.
        # _FakeStdoutOverrun simulates this directly.
        oversized = b"X" * 300 + b"\n"
        normal = (json.dumps({"level": "info", "msg": "ok"}) + "\n").encode()
        emitted: list[tuple[str, str]] = []

        class _FakeStdoutOverrun:
            def __init__(self):
                self._phase = 0

            async def readuntil(self, separator: bytes = b"\n") -> bytes:
                if self._phase == 0:
                    self._phase = 1
                    raise asyncio.LimitOverrunError("too long", 300)
                if self._phase == 1:
                    self._phase = 2
                    return oversized[:300]  # readexactly result fed back; unused here
                if self._phase == 2:
                    self._phase = 3
                    return normal
                raise asyncio.IncompleteReadError(b"", None)

            async def readexactly(self, n: int) -> bytes:
                return b"X" * n

        fake_proc = _FakeProc([], returncode=0)
        fake_proc.stdout = _FakeStdoutOverrun()

        async def _create(*_a, **_k):
            return fake_proc

        with (
            patch.object(
                dlf,
                "_make_emitter",
                return_value=(lambda lvl, msg: emitted.append((lvl, msg)), None),
            ),
            patch.object(dlf.asyncio, "create_subprocess_exec", _create),
            patch.object(dlf, "_install_signal_forwarding"),
            patch.object(dlf, "_drain_and_flush", new=_async_noop),
        ):
            rc = asyncio.run(dlf._run(["daprd"]))

        assert rc == 0
        assert emitted[0][0] == "warning"
        assert "truncated" in emitted[0][1]
        assert emitted[1] == ("info", "ok")

    def test_drains_and_flushes_on_exit(self):
        fake_proc = _FakeProc([], returncode=3)
        flushed: list[bool] = []

        async def _create(*_a, **_k):
            return fake_proc

        async def _drain(_complete):
            flushed.append(True)

        with (
            patch.object(dlf, "_make_emitter", return_value=(lambda *a: None, None)),
            patch.object(dlf.asyncio, "create_subprocess_exec", _create),
            patch.object(dlf, "_install_signal_forwarding"),
            patch.object(dlf, "_drain_and_flush", new=_drain),
        ):
            rc = asyncio.run(dlf._run(["daprd"]))

        assert rc == 3
        assert flushed == [True]

    def test_active_path_forwards_lines_through_sdk_logger_and_drains(self):
        # SDR-active path: with a REAL _make_emitter (not stubbed), daprd's JSON
        # log lines must be routed through the SDK ``dapr.runtime`` logger, and
        # the loop must drain loguru's async sink + flush buffered records on
        # exit so the lines actually reach the lakehouse upload buffer.
        lines = [
            (
                json.dumps({"level": "warning", "msg": "w", "scope": "s"}) + "\n"
            ).encode(),
            (json.dumps({"level": "error", "msg": "e"}) + "\n").encode(),
        ]
        fake_proc = _FakeProc(lines, returncode=0)

        fake_logger = MagicMock()
        # ``_complete`` awaits ``logger.logger.complete()`` — make it awaitable.
        fake_logger.logger.complete = AsyncMock()

        async def _create(*_a, **_k):
            return fake_proc

        with (
            patch(
                "application_sdk.observability.logger_adaptor.get_logger",
                return_value=fake_logger,
            ),
            patch.object(dlf.asyncio, "create_subprocess_exec", _create),
            patch.object(dlf, "_install_signal_forwarding"),
            patch(
                "application_sdk.observability.observability.AtlanObservability.flush_all",
                new_callable=AsyncMock,
            ) as flush_all_mock,
        ):
            rc = asyncio.run(dlf._run(["daprd"]))

        assert rc == 0
        # Each line was forwarded to the matching SDK logger level method.
        fake_logger.warning.assert_called_once_with("[s] w")
        fake_logger.error.assert_called_once_with("e")
        # On exit the async sink was drained and buffered records flushed.
        fake_logger.logger.complete.assert_awaited()
        flush_all_mock.assert_awaited()


async def _async_noop(*_args, **_kwargs):
    return None


# A stand-in for daprd: the forwarder's child is whatever follows ``--``, so the
# real sidecar binary is not needed to exercise the level gate end to end. Emits
# one line at each of the two levels that matter, tagged so the assertions cannot
# match the SDK's own startup chatter.
_FAKE_DAPRD = """
import time

print(
    '{"level":"debug","msg":"CANARY-daprd-debug","scope":"dapr.runtime.http","time":"t"}',
    flush=True,
)
print(
    '{"level":"info","msg":"CANARY-daprd-info","scope":"dapr.runtime","time":"t"}',
    flush=True,
)
time.sleep(0.05)
"""


class TestForwardedLevelsObserved:
    """The gate observed, not computed.

    Every other test here injects a ``MagicMock`` logger, so ``logger.debug()``
    is always "called" whether or not the real sinks would admit the record —
    the thing this feature exists to change. These run the forwarder as a real
    process against a fake daprd and read what actually came out, so they fail
    if the SDK stops honouring ``ATLAN_LOG_LEVEL``, if a sink starts reading a
    different variable, or if the re-exec silently stops happening.

    A subprocess because the level binds at import, before any test can patch it
    — the same reason ``TestDiagnoseGating`` in ``test_logger_adaptor.py`` is
    driven this way.
    """

    def _run_forwarder(self, tmp_path: Path, env_overrides: dict[str, str]) -> str:
        fake_daprd = tmp_path / "fake_daprd.py"
        fake_daprd.write_text(_FAKE_DAPRD, encoding="utf-8")
        env = {
            **os.environ,
            # Console sinks only: no exporter, no object store, no network.
            "ENABLE_OTLP_LOGS": "false",
            "ATLAN_ENABLE_OBSERVABILITY_STORE_SINK": "false",
            # SDR mode — the forwarding path. Outside it main() execs daprd
            # transparently and nothing reaches the SDK logger at all.
            "ENABLE_ATLAN_UPLOAD": "true",
        }
        # An ambient level from the shell or image would decide the outcome
        # instead of the case under test; ATLAN_LOG_LEVEL is popped rather than
        # overwritten so LOG_LEVEL is the app level unless a case says otherwise.
        env.pop("ATLAN_LOG_LEVEL", None)
        env["LOG_LEVEL"] = "INFO"
        env.update(env_overrides)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "application_sdk.observability.dapr_log_forwarder",
                "--",
                sys.executable,
                str(fake_daprd),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[3]),
            timeout=120,
        )
        out = result.stdout + result.stderr
        # Control: the info line must always arrive, or a case below could pass
        # for the wrong reason (forwarder never ran, fake daprd never spawned).
        assert (
            "CANARY-daprd-info" in out
        ), f"forwarder produced no daprd lines at all:\n{out}"
        return out

    @staticmethod
    def _line_for(out: str, canary: str) -> str:
        return next(line for line in out.splitlines() if canary in line)

    @pytest.mark.skipif(
        os.name != "posix",
        reason="the re-exec is POSIX-only: Windows os.execve spawns a copy and "
        "exits the parent, so the forwarded lines never reach this process's "
        "pipes. The forwarder ships only in the Linux container image.",
    )
    def test_dapr_debug_reaches_the_pipeline_as_a_debug_record(self, tmp_path: Path):
        """The original bug: with the app at INFO, a daprd debug line was gated
        out of the console *and* the lakehouse even though the operator had asked
        for it. It must now arrive, and arrive at DEBUG — not folded into the
        message or promoted to the app's level."""
        out = self._run_forwarder(tmp_path, {"DAPR_LOG_LEVEL": "debug"})
        assert "CANARY-daprd-debug" in out
        assert "[DEBUG]" in self._line_for(out, "CANARY-daprd-debug")

    def test_app_level_still_gates_when_daprd_is_not_more_verbose(self, tmp_path: Path):
        """The two knobs stay independent in the other direction: DAPR_LOG_LEVEL
        at the image default does not lower the gate, so a debug line below it is
        still dropped. Without this, the test above would pass just as well if the
        forwarder had stopped gating entirely."""
        out = self._run_forwarder(tmp_path, {"DAPR_LOG_LEVEL": "info"})
        assert "CANARY-daprd-debug" not in out

    @pytest.mark.skipif(
        os.name != "posix",
        reason="quietening the app to ERROR only forwards daprd's info line via "
        "the POSIX-only re-exec; see the skip above.",
    )
    def test_quieting_the_app_never_promotes_a_daprd_line(self, tmp_path: Path):
        """LOG_LEVEL=ERROR with the image default DAPR_LOG_LEVEL=info: daprd's
        info line passes *as INFO*. A regression that re-derived the emitted
        level from the app's level would manufacture ERROR records here, and
        reach error-rate dashboards and alert rules keyed on SDK error volume."""
        out = self._run_forwarder(
            tmp_path, {"LOG_LEVEL": "ERROR", "DAPR_LOG_LEVEL": "info"}
        )
        line = self._line_for(out, "CANARY-daprd-info")
        assert "[INFO]" in line
        assert "[ERROR]" not in line

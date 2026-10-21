"""Tests for the daprd flags ``entrypoint.sh`` passes.

Runs the real script under bash with a stub ``daprd`` on ``PATH`` that records
its arguments and exits, so the script stops at its startup check. bash, like
the busybox sh in the image, accepts the script's ``SIGTERM`` trap names; dash
(``/bin/sh`` on Debian/Ubuntu) does not.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="entrypoint.sh is a POSIX shell script; needs bash",
)


def _run_entrypoint(
    tmp_path: Path, env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    args_file = tmp_path / "daprd-args"
    stub = tmp_path / "daprd"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\nexit 1\n')
    stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, args_file


def _daprd_args(tmp_path: Path, env: dict[str, str] | None = None) -> list[str]:
    _, args_file = _run_entrypoint(tmp_path, env)
    return args_file.read_text().splitlines()


def _flag_value(args: list[str], flag: str) -> str:
    assert flag in args, f"daprd was started without {flag}: {args}"
    return args[args.index(flag) + 1]


def test_pins_internal_grpc_port_apart_from_api_port(tmp_path: Path) -> None:
    args = _daprd_args(tmp_path)

    assert _flag_value(args, "--dapr-internal-grpc-port") == "50002"
    assert _flag_value(args, "--dapr-grpc-port") == "50001"


def test_internal_grpc_port_follows_overridden_api_port(tmp_path: Path) -> None:
    args = _daprd_args(tmp_path, {"DAPR_GRPC_PORT": "50002"})

    assert _flag_value(args, "--dapr-grpc-port") == "50002"
    assert _flag_value(args, "--dapr-internal-grpc-port") == "50003"


def test_forwards_internal_grpc_port_override(tmp_path: Path) -> None:
    args = _daprd_args(tmp_path, {"DAPR_INTERNAL_GRPC_PORT": "51234"})

    assert _flag_value(args, "--dapr-internal-grpc-port") == "51234"


@pytest.mark.parametrize(
    "env",
    [
        {"DAPR_INTERNAL_GRPC_PORT": "50001"},
        {"DAPR_GRPC_PORT": "50005", "DAPR_INTERNAL_GRPC_PORT": "50005"},
        {"DAPR_INTERNAL_GRPC_PORT": "3500"},
        {"DAPR_INTERNAL_GRPC_PORT": "3100"},
    ],
    ids=["default-api", "overridden-api", "http", "metrics"],
)
def test_refuses_internal_grpc_port_clash(tmp_path: Path, env: dict[str, str]) -> None:
    result, args_file = _run_entrypoint(tmp_path, env)

    assert result.returncode == 1
    assert "clashes with another daprd port" in result.stderr
    assert not args_file.exists(), "daprd must not start with a clashing port"


def test_kills_daprd_that_ignores_sigterm_after_app_exits(tmp_path: Path) -> None:
    """A daprd that outlives the app must not hold the container up."""
    daprd = tmp_path / "daprd"
    daprd.write_text("#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n")
    daprd.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "DAPR_STOP_TIMEOUT_SECONDS": "1",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "daprd still running after 1s, killing it" in result.stdout


def _run_entrypoint_and_sigterm(tmp_path: Path, app_exit_on_term: int) -> int:
    """Start the entrypoint, SIGTERM it once the app runs, return its exit code."""
    daprd = tmp_path / "daprd"
    daprd.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n")
    daprd.chmod(0o755)
    ready = tmp_path / "app-ready"
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!/bin/sh\ntrap 'exit {app_exit_on_term}' TERM\n"
        f"touch '{ready}'\n"
        "while :; do sleep 1; done\n"
    )
    uv.chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(ENTRYPOINT)],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "DAPR_STOP_TIMEOUT_SECONDS": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Signal only once the stub app has installed its TERM trap; earlier,
        # the default action kills it with 143 instead of the code under test.
        deadline = time.monotonic() + 15
        while not ready.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail("entrypoint did not start the app")
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        return proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_sigterm_path_passes_through_post_drain_exit_code(tmp_path: Path) -> None:
    """An app cut short by its post-drain deadline (code 3) is not reported as 0."""
    assert _run_entrypoint_and_sigterm(tmp_path, app_exit_on_term=3) == 3


def test_sigterm_path_graceful_stop_exits_zero(tmp_path: Path) -> None:
    assert _run_entrypoint_and_sigterm(tmp_path, app_exit_on_term=0) == 0

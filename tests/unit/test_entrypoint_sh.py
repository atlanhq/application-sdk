"""Tests for the daprd flags ``entrypoint.sh`` passes.

Runs the real script with a stub ``daprd`` on ``PATH`` that records its
arguments and exits, so the script stops at its startup check. The image's
``/bin/sh`` is busybox ash and has no bash (``Dockerfile`` removes it), so every
script-running test runs under each POSIX shell found here: bash, ``sh`` and
dash (``/bin/sh`` on Debian/Ubuntu CI runners).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"

# The daprd flag each Dockerfile-owned DAPR_* variable is handed to.
_DAPRD_FLAG = {
    "DAPR_APP_ID": "--app-id",
    "DAPR_HTTP_PORT": "--dapr-http-port",
    "DAPR_GRPC_PORT": "--dapr-grpc-port",
    "DAPR_LOG_LEVEL": "--log-level",
    "DAPR_METRICS_PORT": "--metrics-port",
    "DAPR_MAX_BODY_SIZE": "--max-body-size",
    "DAPR_GRACEFUL_SHUTDOWN_SECONDS": "--dapr-graceful-shutdown-seconds",
}

_SHELLS = [s for s in ("bash", "sh", "dash") if shutil.which(s)]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not _SHELLS,
    reason="entrypoint.sh is a POSIX shell script; needs a POSIX shell",
)


@pytest.fixture(params=_SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    """Each POSIX shell available here; the script must run under all of them."""
    return request.param


def _run_entrypoint(
    tmp_path: Path, shell: str, env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    args_file = tmp_path / "daprd-args"
    stub = tmp_path / "daprd"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\nexit 1\n')
    stub.chmod(0o755)

    result = subprocess.run(
        [shell, str(ENTRYPOINT)],
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, args_file


def _daprd_args(
    tmp_path: Path, shell: str, env: dict[str, str] | None = None
) -> list[str]:
    _, args_file = _run_entrypoint(tmp_path, shell, env)
    return args_file.read_text().splitlines()


def _flag_value(args: list[str], flag: str) -> str:
    assert flag in args, f"daprd was started without {flag}: {args}"
    return args[args.index(flag) + 1]


def test_pins_internal_grpc_port_apart_from_api_port(
    tmp_path: Path, shell: str
) -> None:
    args = _daprd_args(tmp_path, shell)

    assert _flag_value(args, "--dapr-internal-grpc-port") == "50002"
    assert _flag_value(args, "--dapr-grpc-port") == "50001"


def test_internal_grpc_port_follows_overridden_api_port(
    tmp_path: Path, shell: str
) -> None:
    args = _daprd_args(tmp_path, shell, {"DAPR_GRPC_PORT": "50002"})

    assert _flag_value(args, "--dapr-grpc-port") == "50002"
    assert _flag_value(args, "--dapr-internal-grpc-port") == "50003"


def test_forwards_internal_grpc_port_override(tmp_path: Path, shell: str) -> None:
    args = _daprd_args(tmp_path, shell, {"DAPR_INTERNAL_GRPC_PORT": "51234"})

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
def test_refuses_internal_grpc_port_clash(
    tmp_path: Path, shell: str, env: dict[str, str]
) -> None:
    result, args_file = _run_entrypoint(tmp_path, shell, env)

    assert result.returncode == 1
    assert "clashes with another daprd port" in result.stderr
    assert not args_file.exists(), "daprd must not start with a clashing port"


def test_kills_daprd_that_ignores_sigterm_after_app_exits(
    tmp_path: Path, shell: str
) -> None:
    """A daprd that outlives the app must not hold the container up."""
    daprd = tmp_path / "daprd"
    daprd.write_text("#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n")
    daprd.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)

    result = subprocess.run(
        [shell, str(ENTRYPOINT)],
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


def _run_entrypoint_with_failing_app(
    tmp_path: Path, shell: str, app_exit: int
) -> subprocess.CompletedProcess[str]:
    """Run the normal (no-signal) path with an app that exits ``app_exit``."""
    daprd_pid = tmp_path / "daprd-pid"
    daprd = tmp_path / "daprd"
    # Detach from the captured pipes: on a failing exit the script leaves daprd
    # to the container teardown, and an inherited pipe would hold run() open.
    daprd.write_text(
        f"#!/bin/sh\necho $$ > '{daprd_pid}'\nexec >/dev/null 2>&1\n"
        "while :; do sleep 1; done\n"
    )
    daprd.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/sh\nexit {app_exit}\n")
    uv.chmod(0o755)

    try:
        return subprocess.run(
            [shell, str(ENTRYPOINT)],
            env={
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "DAPR_STOP_TIMEOUT_SECONDS": "1",
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        if daprd_pid.exists():
            try:
                os.kill(int(daprd_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_app_killed_by_sigkill_prints_oom_hint(tmp_path: Path, shell: str) -> None:
    """Exit 137 is almost always an OOM kill; the operator hint must reach the log."""
    result = _run_entrypoint_with_failing_app(tmp_path, shell, app_exit=137)

    assert result.returncode == 137
    assert "App exited with code 137" in result.stdout
    assert "CRITICAL: exit code 137 = SIGKILL" in result.stderr


def test_failing_app_exit_is_logged_and_passed_through(
    tmp_path: Path, shell: str
) -> None:
    result = _run_entrypoint_with_failing_app(tmp_path, shell, app_exit=1)

    assert result.returncode == 1
    assert "App exited with code 1" in result.stdout
    assert "CRITICAL" not in result.stderr
    # A crashed app has nothing in flight; don't spend the daprd stop timeout.
    assert "Stopping daprd" not in result.stdout


def _run_entrypoint_and_sigterm(
    tmp_path: Path, shell: str, app_exit_on_term: int
) -> int:
    """Start the entrypoint, SIGTERM it once the app runs, return its exit code."""
    daprd = tmp_path / "daprd"
    daprd.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n")
    daprd.chmod(0o755)
    ready = tmp_path / "app-ready"
    uv = tmp_path / "uv"
    # `sleep & wait`, not a foreground `sleep`: sh runs a trap only once the
    # foreground command returns, but `wait` returns as soon as the signal
    # lands, so the trap fires on delivery instead of up to a second later.
    uv.write_text(
        f"#!/bin/sh\ntrap 'exit {app_exit_on_term}' TERM\n"
        f"touch '{ready}'\n"
        'while :; do sleep 1 & wait "$!"; done\n'
    )
    uv.chmod(0o755)

    proc = subprocess.Popen(
        [shell, str(ENTRYPOINT)],
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


def test_sigterm_path_passes_through_post_drain_exit_code(
    tmp_path: Path, shell: str
) -> None:
    """An app cut short by its post-drain deadline (code 3) is not reported as 0."""
    assert _run_entrypoint_and_sigterm(tmp_path, shell, app_exit_on_term=3) == 3


def test_sigterm_path_graceful_stop_exits_zero(tmp_path: Path, shell: str) -> None:
    assert _run_entrypoint_and_sigterm(tmp_path, shell, app_exit_on_term=0) == 0


def _dockerfile_dapr_env() -> dict[str, str]:
    """``DAPR_*`` values the image's ``ENV`` instructions set."""
    text = DOCKERFILE.read_text().replace("\\\n", " ")
    env: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("ENV "):
            env.update(re.findall(r"(DAPR_\w+)=(\S+)", line))
    return env


def test_every_dockerfile_dapr_var_maps_to_a_daprd_flag() -> None:
    """A new ``DAPR_*`` ``ENV`` line must be added to the drift check below."""
    dockerfile_env = _dockerfile_dapr_env()

    assert "DAPR_APP_ID" in dockerfile_env, "Dockerfile ENV parse found nothing"
    assert set(dockerfile_env) == set(_DAPRD_FLAG)


def test_unset_defaults_match_dockerfile_env(tmp_path: Path, shell: str) -> None:
    """The Dockerfile ``ENV`` owns these defaults; the script's own fallbacks
    must resolve to the same values, or a run outside the image (and any
    reader of the script) sees a default the image never uses."""
    args = _daprd_args(tmp_path, shell)

    for var, value in _dockerfile_dapr_env().items():
        assert _flag_value(args, _DAPRD_FLAG[var]) == value, var


def test_app_id_does_not_fall_back_to_service_name(tmp_path: Path, shell: str) -> None:
    """Inside the image ``DAPR_APP_ID`` is always set, so the app-id never came
    from ``ATLAN_SERVICE_NAME``; outside it, it must not either."""
    args = _daprd_args(tmp_path, shell, {"ATLAN_SERVICE_NAME": "some-service"})

    assert _flag_value(args, "--app-id") == "app"


def test_dapr_log_forwarder_fallback_matches_dockerfile_env() -> None:
    """The forwarder mirrors the entrypoint's ``DAPR_LOG_LEVEL`` fallback."""
    from application_sdk.observability import dapr_log_forwarder

    assert (
        dapr_log_forwarder._ENTRYPOINT_DAPR_LOG_LEVEL_FALLBACK
        == _dockerfile_dapr_env()["DAPR_LOG_LEVEL"]
    )

"""Tests for the daprd flags ``entrypoint.sh`` passes.

Runs the real script under bash with a stub ``daprd`` on ``PATH`` that records
its arguments and exits, so the script stops at its startup check. bash, like
the busybox sh in the image, accepts the script's ``SIGTERM`` trap names; dash
(``/bin/sh`` on Debian/Ubuntu) does not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="entrypoint.sh is a POSIX shell script; needs bash",
)


def _daprd_args(tmp_path: Path) -> list[str]:
    args_file = tmp_path / "daprd-args"
    stub = tmp_path / "daprd"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\nexit 1\n')
    stub.chmod(0o755)

    subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        timeout=30,
    )
    return args_file.read_text().splitlines()


def _flag_value(args: list[str], flag: str) -> str:
    assert flag in args, f"daprd was started without {flag}: {args}"
    return args[args.index(flag) + 1]


def test_pins_internal_grpc_port_apart_from_api_port(tmp_path: Path) -> None:
    args = _daprd_args(tmp_path)

    assert _flag_value(args, "--dapr-internal-grpc-port") == "50002"
    assert _flag_value(args, "--dapr-grpc-port") == "50001"

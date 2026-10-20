"""repin-application-sdk.sh pins the SDK and its api package to the same ref.

The SDK wheel does not ship the api package's files and pins
``atlan-application-sdk-api==<its own version>``. uv ignores the SDK's own
``[tool.uv.sources]``, so a repin that moves only the SDK would resolve the api
package from PyPI: absent before its first release, and stale after it.
"""

from __future__ import annotations

import filecmp
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = [
    ROOT / ".github/actions/connector-integration-tests/repin-application-sdk.sh",
    ROOT / ".github/actions/sdr-e2e/repin-application-sdk.sh",
]
REF = "0123456789abcdef0123456789abcdef01234567"
SDK = 'atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", rev = "old" }'
API = (
    'atlan-application-sdk-api = { git = "https://github.com/atlanhq/application-sdk", '
    'rev = "old", subdirectory = "packages/api" }'
)
HEAD = '[project]\nname = "demo-app"\nversion = "1.0.0"\n'


def _repin(tmp_path: Path, pyproject: str, script: Path = SCRIPTS[0]) -> dict:
    (tmp_path / "pyproject.toml").write_text(pyproject)
    env = {
        **os.environ,
        "SDK_REF": REF,
        "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
    }
    subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env=env, check=True, capture_output=True
    )
    return tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]


def _assert_pinned(sources: dict) -> None:
    assert sources["atlan-application-sdk"] == {
        "git": "https://github.com/atlanhq/application-sdk",
        "rev": REF,
    }
    assert sources["atlan-application-sdk-api"] == {
        "git": "https://github.com/atlanhq/application-sdk",
        "rev": REF,
        "subdirectory": "packages/api",
    }


def test_the_two_copies_are_identical() -> None:
    assert filecmp.cmp(*SCRIPTS, shallow=False)


@pytest.mark.parametrize("script", SCRIPTS, ids=["integration", "sdr-e2e"])
def test_an_existing_sdk_pin_gains_the_api_pin(tmp_path: Path, script: Path) -> None:
    _assert_pinned(_repin(tmp_path, f"{HEAD}\n[tool.uv.sources]\n{SDK}\n", script))


def test_an_app_on_pypi_gets_both_pins(tmp_path: Path) -> None:
    _assert_pinned(_repin(tmp_path, HEAD))


def test_an_existing_api_pin_is_moved_not_duplicated(tmp_path: Path) -> None:
    sources = _repin(tmp_path, f"{HEAD}\n[tool.uv.sources]\n{SDK}\n{API}\n")
    _assert_pinned(sources)
    assert (tmp_path / "pyproject.toml").read_text().count(
        "atlan-application-sdk-api ="
    ) == 1


def test_a_commented_sources_header_is_not_used(tmp_path: Path) -> None:
    _assert_pinned(_repin(tmp_path, f"{HEAD}\n# [tool.uv.sources]\n# {SDK}\n"))


def test_rerunning_is_a_no_op(tmp_path: Path) -> None:
    _repin(tmp_path, f"{HEAD}\n[tool.uv.sources]\n{SDK}\n")
    first = (tmp_path / "pyproject.toml").read_text()
    _repin(tmp_path, first)
    assert (tmp_path / "pyproject.toml").read_text() == first

"""Tests for .github/scripts/container_python_version.py."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import container_python_version as cpv


def _dockerfile(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text(contents, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_dockerfile_version
# ---------------------------------------------------------------------------


def test_parses_golden_tag(tmp_path: Path) -> None:
    df = _dockerfile(tmp_path, "FROM cgr.dev/atlan.com/app-framework-golden:3.13\n")
    assert cpv.parse_dockerfile_version(df) == "3.13"


def test_ignores_unrelated_from_lines(tmp_path: Path) -> None:
    df = _dockerfile(
        tmp_path,
        "FROM python:3.11 AS build\n"
        "FROM cgr.dev/atlan.com/app-framework-golden:3.13\n",
    )
    assert cpv.parse_dockerfile_version(df) == "3.13"


def test_missing_golden_base_raises(tmp_path: Path) -> None:
    df = _dockerfile(tmp_path, "FROM python:3.13-slim\n")
    with pytest.raises(ValueError, match="no 'app-framework-golden"):
        cpv.parse_dockerfile_version(df)


def test_conflicting_versions_raise(tmp_path: Path) -> None:
    df = _dockerfile(
        tmp_path,
        "FROM cgr.dev/atlan.com/app-framework-golden:3.13\n"
        "FROM cgr.dev/atlan.com/app-framework-golden:3.14\n",
    )
    with pytest.raises(ValueError, match="conflicting golden base versions"):
        cpv.parse_dockerfile_version(df)


def test_repo_dockerfile_is_parseable() -> None:
    # Guards the real repo Dockerfile against a format change that would make
    # the SoT unreadable (the whole chain keys off this parse).
    version = cpv.parse_dockerfile_version(cpv._repo_root() / "Dockerfile")
    assert version.count(".") == 1


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Python 3.13.5", "3.13"),
        ("3.13.5", "3.13"),
        ("3.13", "3.13"),
        ("  Python 3.13.0rc1  ", "3.13"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert cpv.normalize(raw) == expected


def test_normalize_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        cpv.normalize("not-a-version")


# ---------------------------------------------------------------------------
# check-interpreter
# ---------------------------------------------------------------------------


def test_check_interpreter_match() -> None:
    assert (
        cpv.main(
            ["check-interpreter", "--expected", "3.13", "--actual", "Python 3.13.5"]
        )
        == 0
    )


def test_check_interpreter_mismatch_fails() -> None:
    # The exact regression: a worker running a different minor than the image.
    assert (
        cpv.main(
            ["check-interpreter", "--expected", "3.13", "--actual", "Python 3.11.9"]
        )
        == 1
    )


# ---------------------------------------------------------------------------
# check-matrix
# ---------------------------------------------------------------------------


def test_check_matrix_present() -> None:
    assert (
        cpv.main(
            ["check-matrix", "--expected", "3.13", "--matrix", "3.11,3.12,3.13,3.14"]
        )
        == 0
    )


def test_check_matrix_absent_fails() -> None:
    assert (
        cpv.main(["check-matrix", "--expected", "3.15", "--matrix", "3.11 3.12 3.13"])
        == 1
    )


def test_print_version_reads_repo_dockerfile(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cpv.main(["print-version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.count(".") == 1


def test_sdr_e2e_default_matches_dockerfile() -> None:
    # The sdr-e2e action's `expected-python-version` default is the E2E source
    # of truth for connectors (which don't ship this SoT script). It must track
    # the SDK Dockerfile so a golden-image bump can't leave connector E2E
    # asserting — and pinning the host harness to — a stale version.
    root = cpv._repo_root()
    dockerfile_version = cpv.parse_dockerfile_version(root / "Dockerfile")
    action = (root / ".github/actions/sdr-e2e/action.yaml").read_text(encoding="utf-8")
    match = re.search(
        r"expected-python-version:.*?default:\s*\"([0-9.]+)\"", action, re.DOTALL
    )
    assert match, "expected-python-version default not found in sdr-e2e action.yaml"
    assert match.group(1) == dockerfile_version

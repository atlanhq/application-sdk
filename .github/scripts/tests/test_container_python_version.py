"""Tests for .github/scripts/container_python_version.py."""

from __future__ import annotations

import json
import re
from pathlib import Path

import container_python_version as cpv
import pytest
from _workflows import job, load_workflow, triggers


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
        "FROM python:3.11 AS build\nFROM cgr.dev/atlan.com/app-framework-golden:3.13\n",
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


@pytest.mark.parametrize("action", ["sdr-e2e", "build-app-image"])
def test_action_python_default_matches_dockerfile(action: str) -> None:
    # These actions' `expected-python-version` defaults are the E2E source of
    # truth for connectors (which don't ship this SoT script). They must track
    # the SDK Dockerfile so a golden-image bump can't leave connector E2E
    # asserting — and pinning the host harness to — a stale version.
    #
    # Parametrised because the value is now declared in TWO places:
    # build-app-image performs the assertion, and sdr-e2e both forwards it and
    # pins the host harness to it. Two defaults are two chances to drift, so
    # both are pinned to the Dockerfile here rather than only the one that
    # happens to do the comparing.
    root = cpv._repo_root()
    dockerfile_version = cpv.parse_dockerfile_version(root / "Dockerfile")
    action_yaml = (root / ".github/actions" / action / "action.yaml").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"expected-python-version:.*?default:\s*\"([0-9.]+)\"", action_yaml, re.DOTALL
    )
    assert match, f"expected-python-version default not found in {action}/action.yaml"
    assert match.group(1) == dockerfile_version


# ---------------------------------------------------------------------------
# The unit matrix's PR-push split (FND-4673)
# ---------------------------------------------------------------------------
#
# A PR push runs the full Python range on Linux and only the container's Python
# on macOS and Windows; the merge queue runs the full grid. The reduced set is
# an `exclude:` list inside a `fromJSON(...)` expression, which no YAML parser
# expands, so these tests expand it the way GitHub does and assert on the legs.

_LINUX = "ubuntu-22.04"
_SPLIT_RE = re.compile(
    r"^\$\{\{ fromJSON\(inputs\.full-unit-grid && '(?P<full>[^']*)' "
    r"\|\| '(?P<reduced>[^']*)'\) \}\}$"
)


def _unit_job() -> dict:
    return job(load_workflow("sdk-tests-reusable.yaml"), "unit")


def _expand(matrix: dict, exclude: list[dict[str, str]]) -> set[tuple[str, str]]:
    """GitHub's expansion: the axes' product minus every fully matching exclude."""
    legs = {(os, py) for os in matrix["os"] for py in matrix["python-version"]}
    return {
        (os, py)
        for os, py in legs
        if not any(
            e.get("os", os) == os and e.get("python-version", py) == py for e in exclude
        )
    }


def _grids() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    matrix = _unit_job()["strategy"]["matrix"]
    match = _SPLIT_RE.match(matrix["exclude"])
    assert match, f"unexpected exclude expression: {matrix['exclude']!r}"
    full = _expand(matrix, json.loads(match["full"]))
    reduced = _expand(matrix, json.loads(match["reduced"]))
    return full, reduced


def test_full_unit_grid_runs_every_os_python_pair() -> None:
    matrix = _unit_job()["strategy"]["matrix"]
    full, _ = _grids()
    assert len(full) == len(matrix["os"]) * len(matrix["python-version"]) == 12


def test_pr_unit_grid_is_linux_range_plus_container_python_elsewhere() -> None:
    matrix = _unit_job()["strategy"]["matrix"]
    container = cpv.parse_dockerfile_version(cpv._repo_root() / "Dockerfile")
    _, reduced = _grids()

    by_os: dict[str, set[str]] = {}
    for os, py in reduced:
        by_os.setdefault(os, set()).add(py)

    assert by_os[_LINUX] == set(matrix["python-version"])
    for os in matrix["os"]:
        if os != _LINUX:
            # Bumping the image's Python without moving this list would leave
            # macOS and Windows testing an interpreter nothing ships.
            assert by_os.get(os) == {container}, (
                f"{os} PR legs {sorted(by_os.get(os, ()))} must be exactly the "
                f"container's Python {container}"
            )
    assert len(reduced) == 6


def test_pr_unit_grid_keeps_the_coverage_leg() -> None:
    # Coverage (comment, fail-under, per-package floors) is measured on one leg;
    # if the PR grid dropped it, PRs would merge with no coverage check at all.
    expr = _unit_job()["env"]["UNIT_COVERAGE_LEG"]
    leg = re.search(
        r"matrix\.os == '([^']+)' && matrix\.python-version == '([^']+)'", expr
    )
    assert leg, expr
    _, reduced = _grids()
    assert (leg[1], leg[2]) in reduced


def test_full_unit_grid_defaults_on() -> None:
    inputs = triggers(load_workflow("sdk-tests-reusable.yaml"))["workflow_call"][
        "inputs"
    ]
    assert inputs["full-unit-grid"]["default"] is True


def test_sdk_gate_reduces_the_grid_only_on_ordinary_pr_pushes() -> None:
    call = job(load_workflow("sdk-gate.yaml"), "sdk-tests")
    assert call["with"]["full-unit-grid"] == (
        "${{ github.event_name != 'pull_request' "
        "|| needs.changes.outputs.force_sdk == 'true' }}"
    )

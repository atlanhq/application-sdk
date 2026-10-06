"""Tests for the Release Gate bootstrap vendors into consumer repos.

A release PR may merge only once e2e has run on it. Until FND-3411 the
only signal was the `e2e` label; now the SDK's Tests Gate consumes the
label when the run finishes and records the verdict as an `e2e` commit
status, so the gate accepts either. The load-bearing properties are that
consuming the label does not redden a release PR whose head passed, and
that a new head (no status yet) re-blocks it.

The script is a bootstrap template rather than an importable module, so
these tests load it from the template directory by path.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import types

import pytest
import yaml
from conformance.bootstrap.render import MANAGED_ACTION_FILES, render

_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "conformance"
    / "bootstrap"
    / "templates"
    / "release_gate.py"
)


@pytest.fixture(scope="module")
def gate() -> types.ModuleType:
    """Load the vendored gate script as a module."""
    spec = importlib.util.spec_from_file_location("release_gate", _TEMPLATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    labels: list[str],
    state: str | None,
) -> int:
    """Run ``main`` with ``labels`` and a state file holding ``state``."""
    state_file = tmp_path / "e2e-state.txt"
    if state is not None:
        state_file.write_text(state)
    return gate.main(
        ["--labels", json.dumps(labels), "--e2e-state-file", str(state_file)],
    )


@pytest.mark.parametrize("state", [None, "", "success", "failure", "pending"])
def test_non_release_pr_always_passes(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    state: str | None,
) -> None:
    """The gate exists only for bump PRs; every other PR passes untouched."""
    assert _run(gate, tmp_path, ["size/S"], state) == 0


@pytest.mark.parametrize("state", [None, "", "failure", "pending"])
def test_release_pr_with_the_label_passes(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    state: str | None,
) -> None:
    """The label alone passes, whatever the status says.

    The label means a run is requested or in flight; its verdict is the
    required Tests Gate's to enforce, exactly as before FND-3411. A stale
    `failure` from an earlier run must not block the re-run the label asks for.
    """
    assert _run(gate, tmp_path, ["release", "e2e"], state) == 0


def test_consumed_label_with_a_passing_status_passes(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
) -> None:
    """The case FND-3411 exists for: label removed after a green run."""
    assert _run(gate, tmp_path, ["release"], "success") == 0


@pytest.mark.parametrize("state", [None, "", "pending"])
def test_release_pr_without_label_or_verdict_fails(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    state: str | None,
) -> None:
    """A new head has no status, so a push re-blocks until the label returns.

    An absent file and an empty one are the failed-lookup signal, and the
    gate is fail-closed on it - it was fail-closed before the status existed.
    """
    assert _run(gate, tmp_path, ["release"], state) == 1


@pytest.mark.parametrize("state", ["failure", "error"])
def test_release_pr_whose_e2e_failed_fails(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """A failed run says so, rather than asking for a label that was there."""
    assert _run(gate, tmp_path, ["release"], state) == 1
    assert "failed" in capsys.readouterr().out


def test_the_newest_state_line_wins(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
) -> None:
    """Trailing blank lines from `--jq` do not hide the state."""
    assert _run(gate, tmp_path, ["release"], "failure\nsuccess\n\n") == 0


def test_unparseable_labels_read_as_none(gate: types.ModuleType) -> None:
    """Garbage in the labels argument cannot be mistaken for `release`."""
    assert gate.parse_labels("not json") == []
    assert gate.parse_labels('{"release": true}') == []
    assert gate.parse_labels('["release", 3]') == ["release"]


def test_output_is_ascii_only() -> None:
    """Vendored into every repo; a non-ASCII byte trips some fleet linters."""
    _TEMPLATE.read_text(encoding="utf-8").encode("ascii")


def test_script_is_vendored_where_the_workflow_calls_it() -> None:
    """The workflow runs `.github/scripts/release_gate.py`; bootstrap writes it."""
    assert (".github/scripts/release_gate.py", "release_gate.py") in (
        MANAGED_ACTION_FILES
    )
    steps = yaml.safe_load(render("release-gate.yaml"))["jobs"]["release-gate"]["steps"]
    verify = next(s for s in steps if s.get("name") == "Verify release readiness")
    assert "python3 .github/scripts/release_gate.py" in verify["run"]


def test_status_lookup_reads_the_head_commit_and_fails_soft() -> None:
    """The status lives on the PR head, not the merge ref the run checks out.

    The lookup must be `continue-on-error`: a failure leaves the file empty
    and the script fails the gate with a readable message, instead of the
    step failing with a bare API error. And without `statuses: read` the
    lookup 403s on every run and no consumed label could ever pass.
    """
    workflow = yaml.safe_load(render("release-gate.yaml"))
    assert workflow["permissions"]["statuses"] == "read"
    steps = workflow["jobs"]["release-gate"]["steps"]
    fetch = next(
        s for s in steps if s.get("name") == "Fetch the head commit's e2e status"
    )
    assert fetch["continue-on-error"] is True
    assert fetch["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'select(.context == "e2e")' in fetch["run"]


def test_status_lookup_walks_every_page() -> None:
    """`e2e` can sit past the first page on a commit with many contexts.

    And `--slurp` must never join `--jq`: gh rejects the pair outright, which
    would leave the state file empty - a release PR blocked on every run.
    """
    steps = yaml.safe_load(render("release-gate.yaml"))["jobs"]["release-gate"]["steps"]
    fetch = next(
        s for s in steps if s.get("name") == "Fetch the head commit's e2e status"
    )
    assert "--paginate" in fetch["run"]
    assert "per_page=100" in fetch["run"]
    assert "--slurp" not in fetch["run"]


def test_gate_reruns_on_unlabeled() -> None:
    """A human removing `e2e` must re-evaluate, not leave a stale green."""
    triggers = yaml.safe_load(render("release-gate.yaml"))[True]
    assert "unlabeled" in triggers["pull_request"]["types"]

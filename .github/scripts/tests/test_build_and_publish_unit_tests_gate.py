"""Tests for .github/scripts/build_and_publish/unit_tests_gate.py."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))

import _bap  # noqa: E402
import unit_tests_gate  # noqa: E402

Responder = Callable[[list[str]], tuple[int, str]]


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    files = {"output": tmp_path / "out", "summary": tmp_path / "summary"}
    for f in files.values():
        f.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(files["output"]))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(files["summary"]))
    for key, value in {
        "REF": "refs/heads/main",
        "DISPATCH_REF": "refs/heads/main",
        "WORKFLOW_FILE": "unit-tests.yaml",
        "REPO": "atlanhq/demo-app",
        "RUN_ID": "42",
    }.items():
        monkeypatch.setenv(key, value)
    return files


def _stub(monkeypatch: pytest.MonkeyPatch, respond: Responder) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        rc, out = respond(list(cmd))
        return subprocess.CompletedProcess(cmd, rc, out, "")

    monkeypatch.setattr(_bap, "run", fake)
    return calls


def _responder(
    *, existing: str = "", dispatch_rc: int = 0, appears_after: int | None = 1
) -> Responder:
    listings = {"n": 0}

    def respond(cmd: list[str]) -> tuple[int, str]:
        if cmd[:2] == ["gh", "api"]:
            return 0, "cafef00d\n"
        if cmd[:3] == ["gh", "run", "list"] and "--commit" in cmd:
            return 0, f"{existing}\n" if existing else "\n"
        if cmd[:3] == ["gh", "workflow", "run"]:
            return dispatch_rc, ""
        if cmd[:3] == ["gh", "run", "list"]:
            listings["n"] += 1
            if appears_after is not None and listings["n"] >= appears_after:
                return 0, "777\n"
            return 0, ""
        raise AssertionError(cmd)

    return respond


def test_existing_run_is_reused(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, _responder(existing="555"))
    assert unit_tests_gate.main(["prepare"]) == 0
    assert gh["output"].read_text() == "run_id=555\n"
    assert calls[0] == [
        "gh",
        "api",
        "/repos/atlanhq/demo-app/commits/refs/heads/main",
        "--jq",
        ".sha",
    ]
    assert "--commit" in calls[1] and "cafef00d" in calls[1]
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in calls)


def test_no_run_dispatches_and_waits_for_it_to_register(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub(monkeypatch, _responder(appears_after=3))
    assert unit_tests_gate.main(["prepare"]) == 0
    assert gh["output"].read_text() == "run_id=777\n"
    dispatch = next(c for c in calls if c[:3] == ["gh", "workflow", "run"])
    assert dispatch[-2:] == ["--ref", "refs/heads/main"]
    poll = calls[-1]
    # `--branch` takes the short name; the full ref would never match.
    assert poll[poll.index("--branch") + 1] == "main"
    assert poll[poll.index("--created") + 1].startswith(">")
    out = capsys.readouterr().out
    assert "Attempt 2/12 — run not yet visible, retrying..." in out
    assert "New run registered after 3 attempt(s): 777" in out


def test_tag_dispatch_ref_is_shortened_for_the_branch_filter(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPATCH_REF", "refs/tags/v1.0.0")
    calls = _stub(monkeypatch, _responder())
    assert unit_tests_gate.main(["prepare"]) == 0
    assert calls[-1][calls[-1].index("--branch") + 1] == "v1.0.0"


def test_failed_dispatch_blocks_with_a_summary(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub(monkeypatch, _responder(dispatch_rc=1))
    assert unit_tests_gate.main(["prepare"]) == 1
    assert "::error title=Publish blocked::Failed to dispatch 'unit-tests.yaml'." in (
        capsys.readouterr().out
    )
    summary = gh["summary"].read_text()
    assert summary.startswith(
        "### Publish blocked: failed to dispatch 'unit-tests.yaml'\n"
    )
    assert "for SHA `cafef00d`" in summary
    assert gh["output"].read_text() == ""


def test_dispatched_run_that_never_registers_fails(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub(monkeypatch, _responder(appears_after=None))
    assert unit_tests_gate.main(["prepare"]) == 1
    assert sum(1 for c in calls if c[:3] == ["gh", "run", "list"]) == 1 + 12
    assert "never registered within 60s" in capsys.readouterr().out
    assert gh["output"].read_text() == ""


def test_gh_failure_ends_the_step_with_its_exit_code(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, lambda cmd: (4, ""))
    assert unit_tests_gate.main(["prepare"]) == 4


# ── wait ──────────────────────────────────────────────────────────────────────


def _views(*states: dict[str, object]) -> Responder:
    it = iter(states)
    return lambda cmd: (0, json.dumps(next(it)))


def test_wait_returns_the_conclusion_once_completed(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub(
        monkeypatch,
        _views(
            {"status": "in_progress", "conclusion": None, "url": "u"},
            {"status": "completed", "conclusion": "failure", "url": "https://run"},
        ),
    )
    assert unit_tests_gate.main(["wait"]) == 0
    assert gh["output"].read_text() == "conclusion=failure\nurl=https://run\n"
    assert calls[0] == [
        "gh",
        "run",
        "view",
        "42",
        "--repo",
        "atlanhq/demo-app",
        "--json",
        "status,conclusion,url",
    ]
    assert "  iteration 1: status=in_progress conclusion=\n" in capsys.readouterr().out


def test_wait_times_out(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub(monkeypatch, lambda cmd: (0, '{"status": "queued", "url": "u"}'))
    assert unit_tests_gate.main(["wait"]) == 1
    assert len(calls) == 70
    assert (
        "Tests run 42 did not complete within the gate timeout."
        in capsys.readouterr().out
    )

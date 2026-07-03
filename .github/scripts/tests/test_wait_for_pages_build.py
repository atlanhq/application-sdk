"""Tests for .github/scripts/wait_for_pages_build.py.

Covers the driver's conditional logic — the part that used to be inlined shell
in contract-toolkit-publish.yml:

  * already built for the target commit -> skip triggering a new build
  * not yet built, then transitions to built -> succeeds without a false skip
  * commit mismatch (stale "latest" build) -> keeps polling instead of exiting
  * errored build -> fails immediately, without exhausting the poll budget
  * never reaches built -> times out after max_attempts

`gh` is stubbed; each stubbed `pages/builds/latest` call returns a single
canned response so status/commit are read together, matching the production
code's single-call-per-poll contract. The target commit is passed explicitly
(--commit), not read from a checkout, matching the driver's contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import wait_for_pages_build as mod

REPO = "atlanhq/application-sdk"
SHA = "9756039c00c9d63becabe6da2bd4f36231857591"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def _build(status: str, commit: str = SHA, error_message: str | None = None) -> dict:
    build = {"status": status, "commit": commit}
    if error_message is not None:
        build["error"] = {"message": error_message}
    return build


def _make_fake_run(build_responses: list[dict]):
    """Dispatch `gh api .../builds/latest` / `gh api POST .../builds`. Each
    `builds/latest` call pops the next canned response, so a test can script
    exactly what each poll iteration sees."""
    calls = {"trigger": 0, "polls": 0}
    remaining = list(build_responses)

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        if cmd[:2] == ["gh", "api"] and cmd[2] == f"repos/{REPO}/pages/builds/latest":
            calls["polls"] += 1
            build = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return _completed(json.dumps(build))
        if cmd[:4] == ["gh", "api", "--method", "POST"]:
            calls["trigger"] += 1
            return _completed("")
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run, calls


def test_already_built_skips_trigger(monkeypatch):
    fake_run, calls = _make_fake_run([_build("built")])
    monkeypatch.setattr(mod, "run", fake_run)

    assert mod.wait_for_build(REPO, SHA, sleep=lambda s: None) is True
    assert calls["trigger"] == 0
    assert calls["polls"] == 1


def test_building_then_built_succeeds(monkeypatch):
    fake_run, calls = _make_fake_run(
        [_build("queued"), _build("building"), _build("built")]
    )
    monkeypatch.setattr(mod, "run", fake_run)

    assert mod.wait_for_build(REPO, SHA, sleep=lambda s: None) is True
    assert calls["trigger"] == 1


def test_stale_commit_keeps_polling_until_target_commit_builds(monkeypatch):
    fake_run, _ = _make_fake_run(
        [
            _build("queued"),
            _build("built", commit="stale-sha"),  # a prior build, not ours
            _build("building"),
            _build("built"),
        ]
    )
    monkeypatch.setattr(mod, "run", fake_run)

    assert mod.wait_for_build(REPO, SHA, sleep=lambda s: None) is True


def test_errored_build_fails_without_exhausting_budget(monkeypatch):
    fake_run, calls = _make_fake_run(
        [_build("queued"), _build("errored", error_message="boom")]
    )
    monkeypatch.setattr(mod, "run", fake_run)

    assert (
        mod.wait_for_build(REPO, SHA, max_attempts=300, sleep=lambda s: None) is False
    )
    # Only polled twice (initial skip-check + the errored poll), not 300 times.
    assert calls["polls"] == 2


def test_timeout_when_never_built(monkeypatch):
    fake_run, calls = _make_fake_run([_build("queued"), _build("building")])
    monkeypatch.setattr(mod, "run", fake_run)

    assert mod.wait_for_build(REPO, SHA, max_attempts=3, sleep=lambda s: None) is False
    # initial skip-check + 3 poll attempts
    assert calls["polls"] == 4


def test_main_exit_codes(monkeypatch):
    fake_run, _ = _make_fake_run([_build("built")])
    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.main(["--repo", REPO, "--commit", SHA]) == 0

    fake_run, _ = _make_fake_run(
        [_build("queued"), _build("errored", error_message="boom")]
    )
    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.main(["--repo", REPO, "--commit", SHA]) == 1

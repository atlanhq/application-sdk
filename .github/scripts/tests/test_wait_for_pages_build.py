"""Tests for .github/scripts/wait_for_pages_build.py.

Covers the driver's conditional logic — the part that used to be inlined shell
in contract-toolkit-publish.yml:

  * already live -> skip triggering a new build
  * not yet live, then goes live -> succeeds without a false skip
  * never goes live -> times out after max_attempts, without over-polling

`curl`/`gh` are stubbed. The driver polls the actual published URL (HTTP 200)
rather than GitHub's internal `gh api .../pages/builds/*` bookkeeping, which
proved unreliable in practice — see the module docstring.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import wait_for_pages_build as mod

REPO = "atlanhq/application-sdk"
URL = "https://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.17.0.zip"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def _make_fake_run(http_codes: list[str]):
    """Dispatch `curl ... -w %{http_code}` / `gh api POST .../builds`. Each
    curl call pops the next canned status code, so a test can script exactly
    what each poll iteration sees."""
    calls = {"trigger": 0, "polls": 0}
    remaining = list(http_codes)

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        if cmd[0] == "curl":
            calls["polls"] += 1
            code = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return _completed(code)
        if cmd[:4] == ["gh", "api", "--method", "POST"]:
            calls["trigger"] += 1
            return _completed("")
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run, calls


def test_already_live_skips_trigger(monkeypatch):
    fake_run, calls = _make_fake_run(["200"])
    monkeypatch.setattr(mod, "run", fake_run)

    assert mod.wait_for_publish(REPO, URL, sleep=lambda s: None) is True
    assert calls["trigger"] == 0
    assert calls["polls"] == 1


def test_not_live_then_live_succeeds(monkeypatch):
    fake_run, calls = _make_fake_run(["404", "404", "200"])
    monkeypatch.setattr(mod, "run", fake_run)

    assert mod.wait_for_publish(REPO, URL, sleep=lambda s: None) is True
    assert calls["trigger"] == 1
    assert calls["polls"] == 3


def test_timeout_when_never_live(monkeypatch):
    fake_run, calls = _make_fake_run(["404"])
    monkeypatch.setattr(mod, "run", fake_run)

    assert (
        mod.wait_for_publish(REPO, URL, max_attempts=3, sleep=lambda s: None) is False
    )
    # initial skip-check + 3 poll attempts
    assert calls["polls"] == 4
    assert calls["trigger"] == 1


def test_heartbeat_does_not_change_outcome(monkeypatch, capsys):
    fake_run, _ = _make_fake_run(["404", "404", "404", "200"])
    monkeypatch.setattr(mod, "run", fake_run)

    assert (
        mod.wait_for_publish(REPO, URL, heartbeat_every=2, sleep=lambda s: None) is True
    )
    assert "Still waiting" in capsys.readouterr().out


def test_main_exit_codes(monkeypatch):
    fake_run, _ = _make_fake_run(["200"])
    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.main(["--repo", REPO, "--url", URL]) == 0

    fake_run, _ = _make_fake_run(["404"])
    monkeypatch.setattr(mod, "run", fake_run)
    assert (
        mod.main(
            [
                "--repo",
                REPO,
                "--url",
                URL,
                "--max-attempts",
                "1",
                "--sleep-seconds",
                "0",
            ]
        )
        == 1
    )

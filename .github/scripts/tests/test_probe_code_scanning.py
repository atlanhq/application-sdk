"""Tests for .github/scripts/probe_code_scanning.py.

FND-1149. The probe decides whether `upload-sarif` runs at all, and it
runs in a workflow that must never fail (Code Scanning marks a tool as
"reporting errors" when its upload workflow fails). So the cases that
matter are the four the eligibility gate has to separate — public,
private+enabled, private+absent-or-disabled, and a probe that could not
read the API at all — plus the invariant that none of them exits
non-zero.

This file exists because the decision used to live in inlined `run:`
shell, where `docs/standards/ci.md` forbids conditional logic precisely
because it cannot be regression-tested: `set -uo pipefail` (no `-e`)
meant a failed `gh api` set `available=false` and left the job green,
making a suppressed upload on an eligible repo indistinguishable from
genuine ineligibility.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from probe_code_scanning import ABSENT, UNKNOWN, decide, main, probe


class _Proc:
    """Minimal stand-in for `subprocess.CompletedProcess`."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _repo_payload(visibility: str, ghas: str | None) -> str:
    payload: dict[str, object] = {"visibility": visibility}
    if ghas is not None:
        payload["security_and_analysis"] = {"advanced_security": {"status": ghas}}
    return json.dumps(payload)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch):
    """Stub the `gh api` call with a caller-supplied result; record argv."""
    calls: list[list[str]] = []

    def install(proc):
        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            if isinstance(proc, Exception):
                raise proc
            return proc

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    return install


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point GITHUB_OUTPUT at a temp file and set GITHUB_REPOSITORY."""
    out = tmp_path / "gh-output"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_REPOSITORY", "atlanhq/atlan-example-app")
    return out


# ---------------------------------------------------------------------------
# decide: the eligibility rule itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "visibility,ghas,expected",
    [
        # Public repos get code scanning inherently and never carry an
        # advanced_security key, so the visibility branch stands alone.
        ("public", ABSENT, True),
        ("public", "disabled", True),
        ("private", "enabled", True),
        ("internal", "enabled", True),
        # The shapes seen across the real fleet on non-public repos.
        ("private", "disabled", False),
        ("private", ABSENT, False),
        ("internal", ABSENT, False),
        # Probe failure resolves to unavailable: fail closed.
        (UNKNOWN, UNKNOWN, False),
    ],
)
def test_decide(visibility: str, ghas: str, expected: bool) -> None:
    assert decide(visibility, ghas) is expected


# ---------------------------------------------------------------------------
# probe: reading the repos endpoint
# ---------------------------------------------------------------------------


def test_probe_public_repo(gh) -> None:
    calls = gh(_Proc(stdout=_repo_payload("public", None)))
    assert probe("atlanhq/atlan-mysql-app") == ("public", ABSENT, "")
    assert calls == [["gh", "api", "repos/atlanhq/atlan-mysql-app"]]


def test_probe_private_repo_with_ghas_enabled(gh) -> None:
    gh(_Proc(stdout=_repo_payload("private", "enabled")))
    assert probe("atlanhq/atlan-example-app") == ("private", "enabled", "")


def test_probe_private_repo_with_ghas_disabled(gh) -> None:
    gh(_Proc(stdout=_repo_payload("private", "disabled")))
    assert probe("atlanhq/atlan-example-app") == ("private", "disabled", "")


def test_probe_private_repo_without_security_block(gh) -> None:
    """A non-admin token sees no `security_and_analysis` key at all."""
    gh(_Proc(stdout=_repo_payload("private", None)))
    assert probe("atlanhq/atlan-example-app") == ("private", ABSENT, "")


def test_probe_reports_api_failure_rather_than_ineligibility(gh) -> None:
    gh(_Proc(returncode=1, stderr="gh: Not Found (HTTP 404)\nmore detail\n"))
    visibility, ghas, error = probe("atlanhq/atlan-example-app")
    assert (visibility, ghas) == (UNKNOWN, UNKNOWN)
    assert "gh: Not Found (HTTP 404)" in error
    assert "more detail" not in error


def test_probe_reports_failure_with_empty_stderr(gh) -> None:
    gh(_Proc(returncode=7))
    _visibility, _ghas, error = probe("atlanhq/atlan-example-app")
    assert "exit status 7" in error


def test_probe_reports_missing_gh_binary(gh) -> None:
    gh(FileNotFoundError("gh"))
    visibility, ghas, error = probe("atlanhq/atlan-example-app")
    assert (visibility, ghas) == (UNKNOWN, UNKNOWN)
    assert "could not run `gh api" in error


def test_probe_reports_non_json_body(gh) -> None:
    gh(_Proc(stdout="<html>502 Bad Gateway</html>"))
    _visibility, _ghas, error = probe("atlanhq/atlan-example-app")
    assert "non-JSON" in error


def test_probe_reports_non_object_body(gh) -> None:
    gh(_Proc(stdout="[]"))
    _visibility, _ghas, error = probe("atlanhq/atlan-example-app")
    assert "returned no object" in error


# ---------------------------------------------------------------------------
# main: the GITHUB_OUTPUT contract and the always-exit-0 invariant
# ---------------------------------------------------------------------------


def test_main_public_repo_writes_available_true(
    gh, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh(_Proc(stdout=_repo_payload("public", None)))
    assert main() == 0
    assert env.read_text() == "available=true\n"
    assert "::notice::" not in capsys.readouterr().out


def test_main_private_enabled_writes_available_true(gh, env: Path) -> None:
    gh(_Proc(stdout=_repo_payload("private", "enabled")))
    assert main() == 0
    assert env.read_text() == "available=true\n"


@pytest.mark.parametrize("ghas", ["disabled", None])
def test_main_ineligible_repo_skips_and_says_why(
    gh, env: Path, capsys: pytest.CaptureFixture[str], ghas
) -> None:
    gh(_Proc(stdout=_repo_payload("private", ghas)))
    assert main() == 0
    assert env.read_text() == "available=false\n"
    notice = capsys.readouterr().out
    assert "::notice::Code scanning unavailable" in notice
    assert "visibility=private" in notice
    assert f"advanced_security={ghas or ABSENT}" in notice


def test_main_api_failure_is_distinguishable_from_ineligibility(
    gh, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gap the inlined shell had: a failed probe read as ineligible.

    It still fails closed — an unverifiable repo does not upload — but
    the log has to say the probe broke, not that the repo lacks the
    feature, or a public repo silently losing its uploads looks exactly
    like the 77 private ones legitimately skipping.
    """
    gh(_Proc(returncode=1, stderr="HTTP 503\n"))
    assert main() == 0
    assert env.read_text() == "available=false\n"
    notice = capsys.readouterr().out
    assert "could not be resolved" in notice
    assert "HTTP 503" in notice
    assert "not a verdict that the repository is ineligible" in notice
    assert "Code scanning unavailable" not in notice


def test_main_without_repository_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("probe must not shell out with no repo"),
    )
    assert main() == 0
    assert env.read_text() == "available=false\n"
    assert "GITHUB_REPOSITORY is unset" in capsys.readouterr().out


def test_main_appends_rather_than_truncating(gh, env: Path) -> None:
    """GITHUB_OUTPUT is shared with any other step writing to it."""
    env.write_text("other=value\n", encoding="utf-8")
    gh(_Proc(stdout=_repo_payload("public", None)))
    assert main() == 0
    assert env.read_text() == "other=value\navailable=true\n"


def test_main_outside_actions_prints_the_assignment(
    gh, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No GITHUB_OUTPUT (a local run) must not raise."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "atlanhq/atlan-mysql-app")
    gh(_Proc(stdout=_repo_payload("public", None)))
    assert main() == 0
    assert "available=true" in capsys.readouterr().out

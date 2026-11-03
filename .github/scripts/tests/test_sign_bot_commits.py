"""sign-bot-commits turns signing on only when it can do so safely, and changes nothing otherwise."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "sign-bot-commits")
)

import sign_bot_commits  # noqa: E402
from sign_bot_commits import main  # noqa: E402


class FakeRunner:
    """Records every command; ssh-keygen succeeds unless `valid_key` is False."""

    def __init__(self, valid_key: bool = True) -> None:
        self.valid_key = valid_key
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if argv[:2] == ["ssh-keygen", "-y"]:
            return subprocess.CompletedProcess(
                argv, 0 if self.valid_key else 1, "ssh-ed25519 AAAA", ""
            )
        if argv[:2] == ["ssh-keygen", "-l"]:
            return subprocess.CompletedProcess(
                argv, 0, "256 SHA256:abc comment (ED25519)\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def git_configs(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["git", "config"]]


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(
        sign_bot_commits.shutil, "which", lambda _: "/usr/bin/ssh-keygen"
    )
    (tmp_path / "temp").mkdir()
    return {
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "GITHUB_ENV": str(tmp_path / "env"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "COMMITTER_NAME": "atlan-ci",
        "COMMITTER_EMAIL": "it@atlan.com",
        "SIGNING_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
    }


def _read(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_a_valid_key_signs_and_sets_only_the_committer(job: dict[str, str]) -> None:
    run = FakeRunner()
    assert main(job, run) == 0
    key_file = Path(job["RUNNER_TEMP"]) / "bot-signing-key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert ["git", "config", "--global", "commit.gpgsign", "true"] in run.git_configs()
    assert [
        "git",
        "config",
        "--global",
        "user.signingkey",
        str(key_file),
    ] in run.git_configs()
    assert (
        _read(job["GITHUB_ENV"])
        == "GIT_COMMITTER_NAME=atlan-ci\nGIT_COMMITTER_EMAIL=it@atlan.com\n"
    )
    assert "GIT_AUTHOR" not in _read(job["GITHUB_ENV"])
    assert "signing=true" in _read(job["GITHUB_OUTPUT"])


def test_without_a_key_nothing_changes(job: dict[str, str]) -> None:
    job["SIGNING_KEY"] = ""
    run = FakeRunner()
    assert main(job, run) == 0
    assert run.calls == []
    assert _read(job["GITHUB_ENV"]) == ""
    assert not (Path(job["RUNNER_TEMP"]) / "bot-signing-key").exists()
    assert "signing=false" in _read(job["GITHUB_OUTPUT"])


def test_an_invalid_key_fails_before_any_config_and_leaves_no_key_on_disk(
    job: dict[str, str],
) -> None:
    run = FakeRunner(valid_key=False)
    assert main(job, run) == 1
    assert run.git_configs() == []
    assert _read(job["GITHUB_ENV"]) == ""
    assert not (Path(job["RUNNER_TEMP"]) / "bot-signing-key").exists()


def test_a_runner_without_ssh_keygen_fails_by_name(
    job: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sign_bot_commits.shutil, "which", lambda _: None)
    assert main(job, FakeRunner()) == 1
    assert "ssh-keygen is not on this runner" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("COMMITTER_NAME", "atlan-ci\nPATH=/attacker/bin"),
        ("COMMITTER_EMAIL", "it@atlan.com\rLD_PRELOAD=/x.so"),
        ("COMMITTER_NAME", "atlan\x00ci"),
        ("COMMITTER_NAME", "   "),
        ("COMMITTER_EMAIL", "not-an-email"),
    ],
)
def test_a_committer_that_could_break_out_of_github_env_is_refused(
    job: dict[str, str], field: str, value: str
) -> None:
    job[field] = value
    run = FakeRunner()
    assert main(job, run) == 1
    assert run.calls == []
    assert _read(job["GITHUB_ENV"]) == ""
    assert not (Path(job["RUNNER_TEMP"]) / "bot-signing-key").exists()


def test_the_key_never_reaches_the_job_log(
    job: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(job, FakeRunner()) == 0
    out = capsys.readouterr().out
    assert "PRIVATE KEY" not in out and "fake" not in out
    assert "SHA256:abc" in out

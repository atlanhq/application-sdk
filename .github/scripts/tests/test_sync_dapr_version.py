"""Tests for .github/scripts/sync_dapr_version.py (check-dapr-version.yaml)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sync_dapr_version as sdv  # noqa: E402

VERSION_PY = '''"""Docs mention the pin::

      ver=$(grep '^__dapr_version' application_sdk/version.py | cut -d'"' -f2)
"""

__version__ = "3.0.0"
__dapr_version: str = "1.19.1"
'''


@pytest.fixture
def version_file(tmp_path: Path) -> Path:
    path = tmp_path / "version.py"
    path.write_text(VERSION_PY)
    return path


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "github_output"
    path.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


def _parse(path: Path) -> dict[str, str]:
    return dict(ln.split("=", 1) for ln in path.read_text().splitlines())


class FakeRun:
    """Records argv; answers by the first matching prefix in ``replies``."""

    def __init__(self, replies: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(
        self, args: list[str], *, check: bool = True, capture: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        code, out = next(
            (v for k, v in self.replies.items() if tuple(args[: len(k)]) == k),
            (0, ""),
        )
        if check and code:
            raise subprocess.CalledProcessError(code, args, out)
        return subprocess.CompletedProcess(args, code, out, None)

    def ran(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


# -- pinned -------------------------------------------------------------------


def test_pinned_reads_the_assignment_not_the_docstring(
    version_file: Path, outputs: Path
) -> None:
    assert sdv.main(["--version-file", str(version_file), "pinned"]) == 0
    assert _parse(outputs) == {"version": "1.19.1"}


def test_pinned_fails_when_the_pin_is_missing(
    tmp_path: Path, outputs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "version.py"
    path.write_text('__version__ = "3.0.0"\n')
    assert sdv.main(["--version-file", str(path), "pinned"]) == 1
    assert "::error::Failed to parse __dapr_version" in capsys.readouterr().out
    assert outputs.read_text() == ""


def test_pinned_fails_on_an_empty_pin(tmp_path: Path, outputs: Path) -> None:
    path = tmp_path / "version.py"
    path.write_text('__dapr_version: str = ""\n')
    assert sdv.main(["--version-file", str(path), "pinned"]) == 1


# -- baked --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Runtime version: 1.16.2\n", "1.16.2"),
        ("v1.17.0-rc.1 (abc)\n", "1.17.0"),
        ("daprd 1.15.4\nother 9.9.9\n", "1.15.4"),
    ],
)
def test_parse_takes_the_first_semver_without_v(raw: str, want: str) -> None:
    assert sdv.parse_daprd_version(raw) == want


def test_parse_rejects_output_without_a_semver() -> None:
    with pytest.raises(sdv.SyncError, match="Could not extract"):
        sdv.parse_daprd_version("exec format error\n")


def test_baked_logs_raw_output_and_tolerates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeRun({("docker", "run"): (1, "Runtime version: v1.16.2\n")})
    monkeypatch.setattr(sdv, "_run", fake)
    assert sdv.main(["baked", "--image", "img:3"]) == 0
    assert fake.ran("docker", "pull", "img:3")
    out = capsys.readouterr().out
    assert (
        "::group::daprd --version (raw)\nRuntime version: v1.16.2\n::endgroup::" in out
    )
    assert "::notice::Base image daprd version: 1.16.2" in out
    assert _parse(outputs) == {"version": "1.16.2"}


def test_baked_fails_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    monkeypatch.setattr(sdv, "_run", FakeRun({("docker", "run"): (0, "garbage")}))
    assert sdv.main(["baked", "--image", "img:3"]) == 1
    assert outputs.read_text() == ""


def test_baked_fails_when_the_pull_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdv, "_run", FakeRun({("docker", "pull"): (1, "")}))
    with pytest.raises(subprocess.CalledProcessError):
        sdv.main(["baked", "--image", "img:3"])


# -- compare ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pinned", "baked", "match", "notice"),
    [
        ("1.16.2", "1.16.2", "true", "in sync with the base image (1.16.2)"),
        ("1.16.2", "1.17.0", "false", "pin 1.16.2 differs from base image 1.17.0"),
    ],
)
def test_compare(
    outputs: Path,
    capsys: pytest.CaptureFixture[str],
    pinned: str,
    baked: str,
    match: str,
    notice: str,
) -> None:
    assert sdv.main(["compare", "--pinned", pinned, "--baked", baked]) == 0
    assert _parse(outputs) == {"pinned": pinned, "baked": baked, "match": match}
    assert notice in capsys.readouterr().out


# -- bump ---------------------------------------------------------------------


def test_bump_rewrites_only_the_pin(version_file: Path) -> None:
    argv = ["--version-file", str(version_file), "bump"]
    assert sdv.main([*argv, "--pinned", "1.19.1", "--baked", "1.20.0"]) == 0
    assert version_file.read_text() == VERSION_PY.replace(
        '__dapr_version: str = "1.19.1"', '__dapr_version: str = "1.20.0"'
    )


def test_bump_fails_when_the_pin_did_not_match(
    version_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--version-file", str(version_file), "bump"]
    assert sdv.main([*argv, "--pinned", "1.0.0", "--baked", "1.20.0"]) == 1
    assert "got '1.19.1', want '1.20.0'" in capsys.readouterr().out
    assert version_file.read_text() == VERSION_PY


def test_bump_does_not_treat_the_pin_as_a_regex(version_file: Path) -> None:
    # `1.19.1` unescaped would also match `1x19y1`; the escaped form must not.
    version_file.write_text('__dapr_version: str = "1x19y1"\n')
    sdv.rewrite_pin(version_file, "1.19.1", "1.20.0")
    assert version_file.read_text() == '__dapr_version: str = "1x19y1"\n'


# -- open-pr ------------------------------------------------------------------

_LS_REMOTE = ("git", "ls-remote")
_DIFF = ("git", "diff", "--cached", "--quiet")
_PR_LIST = ("gh", "pr", "list")
_PR_CREATE = ("gh", "pr", "create")


def _open_pr(
    monkeypatch: pytest.MonkeyPatch,
    version_file: Path,
    replies: dict[tuple[str, ...], tuple[int, str]],
) -> FakeRun:
    fake = FakeRun(replies)
    monkeypatch.setattr(sdv, "_run", fake)
    argv = ["--version-file", str(version_file), "open-pr"]
    rc = sdv.main([*argv, "--pinned", "1.19.1", "--baked", "1.20.0", "--image", "i:3"])
    assert rc == 0
    return fake


def test_new_branch_opens_a_pr(
    monkeypatch: pytest.MonkeyPatch, version_file: Path, outputs: Path
) -> None:
    fake = _open_pr(
        monkeypatch,
        version_file,
        {
            _LS_REMOTE: (2, ""),
            _DIFF: (1, ""),
            _PR_CREATE: (0, "https://github.com/o/r/pull/42\n"),
        },
    )
    assert fake.ran("git", "checkout", "-b", "chore/sync-daprd-1.20.0")
    assert fake.ran("git", "push", "origin", "chore/sync-daprd-1.20.0")
    create = next(c for c in fake.calls if tuple(c[:3]) == _PR_CREATE)
    assert create[create.index("--base") + 1] == "main"
    assert (
        create[create.index("--title") + 1] == "chore(deps): sync daprd pin to 1.20.0"
    )
    body = create[create.index("--body") + 1]
    assert "| Current pin | `1.19.1` |" in body
    assert "| Runtime image | `i:3` |" in body
    assert _parse(outputs) == {"pr_number": "42"}


def test_existing_pr_is_reused_after_push(
    monkeypatch: pytest.MonkeyPatch, version_file: Path, outputs: Path
) -> None:
    fake = _open_pr(
        monkeypatch,
        version_file,
        {_LS_REMOTE: (2, ""), _DIFF: (1, ""), _PR_LIST: (0, "17\n")},
    )
    assert fake.ran("git", "push")
    assert not fake.ran(*_PR_CREATE)
    assert _parse(outputs) == {"pr_number": "17"}


def test_existing_branch_is_checked_out_and_pin_reapplied(
    monkeypatch: pytest.MonkeyPatch, version_file: Path, outputs: Path
) -> None:
    fake = _open_pr(
        monkeypatch,
        version_file,
        {_LS_REMOTE: (0, "sha\trefs/heads/x"), _DIFF: (1, ""), _PR_LIST: (0, "17")},
    )
    assert fake.ran("git", "fetch", "origin", "chore/sync-daprd-1.20.0")
    assert fake.ran("git", "checkout", "chore/sync-daprd-1.20.0")
    assert not fake.ran("git", "checkout", "-b")
    assert '__dapr_version: str = "1.20.0"' in version_file.read_text()


def test_no_change_skips_commit_but_surfaces_the_open_pr(
    monkeypatch: pytest.MonkeyPatch, version_file: Path, outputs: Path
) -> None:
    fake = _open_pr(
        monkeypatch,
        version_file,
        {_LS_REMOTE: (0, ""), _DIFF: (0, ""), _PR_LIST: (0, "17\n")},
    )
    assert not fake.ran("git", "commit")
    assert not fake.ran("git", "push")
    assert _parse(outputs) == {"pr_number": "17"}


def test_no_change_and_no_open_pr_writes_an_empty_number(
    monkeypatch: pytest.MonkeyPatch, version_file: Path, outputs: Path
) -> None:
    _open_pr(monkeypatch, version_file, {_LS_REMOTE: (0, ""), _DIFF: (0, "")})
    # The approve step is gated on pr_number != '', so empty must stay empty.
    assert _parse(outputs) == {"pr_number": ""}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize(("listing", "want"), [("[]", ""), ('[{"number": 7}]', "7")])
def test_pr_list_jq_emits_nothing_for_no_open_pr(
    monkeypatch: pytest.MonkeyPatch, listing: str, want: str
) -> None:
    """`.[0].number` alone prints `null` on an empty list, which reads as a PR
    number and sends the approve step after PR "null". Run the real filter."""
    fake = FakeRun({})
    monkeypatch.setattr(sdv, "_run", fake)
    sdv._open_pr_number("chore/sync-daprd-1.20.0")
    (cmd,) = fake.calls
    jq_filter = cmd[cmd.index("--jq") + 1]
    out = subprocess.run(
        ["jq", jq_filter], input=listing, text=True, capture_output=True, check=True
    ).stdout.strip()
    assert out == want


def test_a_stalled_command_times_out_as_a_step_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdv, "COMMAND_TIMEOUT_S", 0.2)
    with pytest.raises(sdv.SyncError, match=r"`sleep 5` timed out after 0.2s"):
        sdv._run(["sleep", "5"])


def test_docker_pull_gets_the_longer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(args, 0, "", None)

    monkeypatch.setattr(sdv.subprocess, "run", fake_run)
    sdv._run(["docker", "pull", "img:3"])
    assert seen["timeout"] == sdv.PULL_TIMEOUT_S
    sdv._run(["git", "push"])
    assert seen["timeout"] == sdv.COMMAND_TIMEOUT_S

"""Tests for .github/scripts/upsert_release_pr.py.

The behaviour under test is the one that broke atlanhq/atlan-oracle-app#258:
a fixed bump branch is force-pushed on every re-fire, and the PR tracking it
must follow the branch instead of staying frozen at its first title/body.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import upsert_release_pr as mod

REPO = "atlanhq/atlan-oracle-app"
BASE = "main"
HEAD = "bump-version-main"


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


class Gh:
    """Stub for the `gh` seam: records every call, replies per subcommand."""

    def __init__(self, list_replies: list[str]):
        self._list_replies = list(list_replies)
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if self.fail_on and self.fail_on in joined:
            return _fail("boom")
        if cmd[1:3] == ["pr", "list"]:
            return _ok(self._list_replies.pop(0) if self._list_replies else "[]")
        return _ok()

    def of(self, *subcommand: str) -> list[list[str]]:
        n = len(subcommand)
        return [c for c in self.calls if tuple(c[1 : 1 + n]) == subcommand]


def _body_file(tmp_path: Path, text: str = "Version: 0.3.1 → 0.4.0\n") -> Path:
    path = tmp_path / "body.md"
    path.write_text(text, encoding="utf-8")
    return path


def _argv(
    body_file: Path, title: str = "Bump version to 0.4.0", label: str = "release"
):
    return [
        "--repo",
        REPO,
        "--base",
        BASE,
        "--head",
        HEAD,
        "--title",
        title,
        "--body-file",
        str(body_file),
        "--label",
        label,
    ]


def test_creates_pr_with_label_when_none_open(tmp_path, monkeypatch, capsys):
    gh = Gh(
        ["[]", json.dumps([{"number": 258, "title": "x", "body": "", "labels": []}])]
    )
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert mod.main(_argv(_body_file(tmp_path))) == 0

    create = gh.of("pr", "create")
    assert len(create) == 1
    assert "--title" in create[0]
    assert create[0][create[0].index("--title") + 1] == "Bump version to 0.4.0"
    assert "--label" in create[0]
    # The label must exist before `gh pr create --label` can attach it.
    assert gh.of("label", "create"), "expected the release label to be ensured"
    assert not gh.of("pr", "edit")
    assert "pr_action=created" in capsys.readouterr().out


def test_stale_title_and_body_are_synced_not_skipped(tmp_path, monkeypatch, capsys):
    """The oracle#258 regression: title says 0.3.2, branch is at 0.4.0."""
    open_pr = [
        {
            "number": 258,
            "title": "Bump version to 0.3.2",
            "body": "Version: 0.3.1 → 0.3.2",
            "labels": [{"name": "release"}],
        }
    ]
    gh = Gh([json.dumps(open_pr)])
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert mod.main(_argv(_body_file(tmp_path))) == 0

    assert not gh.of("pr", "create"), "must not try to re-create an open PR"
    edits = gh.of("pr", "edit")
    assert len(edits) == 1
    assert edits[0][3] == "258"
    assert edits[0][edits[0].index("--title") + 1] == "Bump version to 0.4.0"
    assert "--body-file" in edits[0]
    assert "pr_action=updated" in capsys.readouterr().out


def test_no_edit_when_pr_already_matches(tmp_path, monkeypatch, capsys):
    """No spurious `pull_request: edited` webhook on an already-correct PR."""
    body = "Version: 0.3.1 → 0.4.0\n"
    open_pr = [
        {
            "number": 258,
            "title": "Bump version to 0.4.0",
            # GitHub hands back CRLF for bodies stored from a CRLF source.
            "body": body.replace("\n", "\r\n"),
            "labels": [{"name": "release"}],
        }
    ]
    gh = Gh([json.dumps(open_pr)])
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert mod.main(_argv(_body_file(tmp_path, body))) == 0

    assert not gh.of("pr", "edit")
    assert not gh.of("pr", "create")
    assert "pr_action=unchanged" in capsys.readouterr().out


def test_missing_label_is_re_added_on_the_update_path(tmp_path, monkeypatch):
    """tag-and-release.yaml gates on the label; an unlabelled merge ships nothing."""
    body = "Version: 0.3.1 → 0.4.0\n"
    open_pr = [
        {
            "number": 258,
            "title": "Bump version to 0.4.0",
            "body": body,
            "labels": [],
        }
    ]
    gh = Gh([json.dumps(open_pr)])
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert mod.main(_argv(_body_file(tmp_path, body))) == 0

    edits = gh.of("pr", "edit")
    assert len(edits) == 1
    assert "--add-label" in edits[0]
    assert edits[0][edits[0].index("--add-label") + 1] == "release"


def test_empty_label_skips_all_label_work(tmp_path, monkeypatch):
    gh = Gh(["[]", json.dumps([{"number": 9, "title": "", "body": "", "labels": []}])])
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert mod.main(_argv(_body_file(tmp_path), label="")) == 0

    assert not gh.of("label", "create")
    assert "--label" not in " ".join(gh.of("pr", "create")[0])


def test_edit_failure_is_loud(tmp_path, monkeypatch):
    """A failed sync must fail the job, not leave a green run with a stale title."""
    open_pr = [
        {"number": 258, "title": "old", "body": "old", "labels": [{"name": "release"}]}
    ]
    gh = Gh([json.dumps(open_pr)])
    gh.fail_on = "pr edit"
    monkeypatch.setattr(mod, "run", gh)

    with pytest.raises(SystemExit) as excinfo:
        mod.main(_argv(_body_file(tmp_path)))
    assert "::error::" in str(excinfo.value)


def test_list_failure_is_loud(tmp_path, monkeypatch):
    gh = Gh([])
    gh.fail_on = "pr list"
    monkeypatch.setattr(mod, "run", gh)

    with pytest.raises(SystemExit) as excinfo:
        mod.main(_argv(_body_file(tmp_path)))
    assert "::error::" in str(excinfo.value)


def test_label_create_failure_is_tolerated(tmp_path, monkeypatch):
    """`gh label create` fails with "already exists" on every repo but the first."""
    gh = Gh(["[]", json.dumps([{"number": 1, "title": "", "body": "", "labels": []}])])
    gh.fail_on = "label create"
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert mod.main(_argv(_body_file(tmp_path))) == 0
    assert gh.of("pr", "create")


def test_repo_defaults_to_github_repository(tmp_path, monkeypatch):
    gh = Gh([json.dumps([{"number": 7, "title": "t", "body": "b", "labels": []}])])
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    argv = [
        "--base",
        BASE,
        "--head",
        HEAD,
        "--title",
        "t",
        "--body-file",
        str(_body_file(tmp_path, "b")),
        "--label",
        "",
    ]
    assert mod.main(argv) == 0
    assert gh.of("pr", "list")[0][gh.of("pr", "list")[0].index("--repo") + 1] == REPO


def test_outputs_written_to_github_output(tmp_path, monkeypatch):
    out = tmp_path / "gh_out"
    gh = Gh([json.dumps([{"number": 42, "title": "old", "body": "old", "labels": []}])])
    monkeypatch.setattr(mod, "run", gh)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    assert mod.main(_argv(_body_file(tmp_path))) == 0

    written = out.read_text(encoding="utf-8")
    assert "pr_number=42" in written
    assert "pr_action=updated" in written

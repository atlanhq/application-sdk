"""Tests for .github/scripts/signed_commit.py.

The org's signed-commits ruleset blocks a PR carrying an unsigned commit, and a
bot's ``git commit`` + ``git push`` is unsigned. These pin that the helper sends
the local commit's exact tree change through ``createCommitOnBranch`` (the path
GitHub signs), keeps each caller's overwrite guarantee, and publishes nothing
when nothing changed.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import _gh_refs  # noqa: E402
import signed_commit  # noqa: E402
from _gh_refs import Response  # noqa: E402

REPO = "atlanhq/atlan-example-app"
BRANCH = "bump-version-main"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')
    (tmp_path / "old.txt").write_text("gone soon\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit_change(repo: Path) -> tuple[str, str]:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pyproject.toml").write_text('version = "1.0.1"\n')
    (repo / "logo.bin").write_bytes(b"\x00\xff\x10binary")
    (repo / "old.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: bump version to 1.0.1\n\nDetails here.")
    return base, _git(repo, "rev-parse", "HEAD")


class FakeApi:
    """Records gh_request calls and answers them from a script."""

    def __init__(
        self, branch_head: str | None, commit_response: Response | None = None
    ):
        self.branch_head = branch_head
        self.commit_response = commit_response or Response(
            200,
            {},
            {"data": {"createCommitOnBranch": {"commit": {"oid": "f00d", "url": "u"}}}},
        )
        self.calls: list[tuple[str, str, dict | None, bool]] = []

    def __call__(self, method, path, payload=None, *, sleep=None, body_via_file=False):
        self.calls.append((method, path, payload, body_via_file))
        if method == "GET":
            if self.branch_head is None:
                return Response(404, {}, {"message": "Not Found"})
            return Response(200, {}, {"object": {"sha": self.branch_head}})
        if path == "graphql":
            return self.commit_response
        return Response(200 if method == "PATCH" else 201, {}, {})


def _graphql_input(api: FakeApi) -> dict:
    (call,) = [c for c in api.calls if c[1] == "graphql"]
    return call[2]["variables"]["input"]


def test_the_commit_carries_exactly_the_local_change_including_binary_and_deletions(
    repo,
):
    base, _ = _commit_change(repo)
    changes = signed_commit.file_changes(base, "HEAD", repo)
    added = {a["path"]: base64.b64decode(a["contents"]) for a in changes["additions"]}
    assert added == {
        "pyproject.toml": b'version = "1.0.1"\n',
        "logo.bin": b"\x00\xff\x10binary",
    }
    assert changes["deletions"] == [{"path": "old.txt"}]


def test_expect_head_publishes_one_signed_commit_on_that_head(
    repo, monkeypatch, tmp_path
):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=base)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    rc = signed_commit.main(
        [
            "push",
            "--repo",
            REPO,
            "--branch",
            BRANCH,
            "--expect-head",
            base,
            "--cwd",
            str(repo),
        ]
    )
    assert rc == 0
    payload = _graphql_input(api)
    assert payload["expectedHeadOid"] == base
    assert payload["branch"] == {"repositoryNameWithOwner": REPO, "branchName": BRANCH}
    assert payload["message"] == {
        "headline": "chore: bump version to 1.0.1",
        "body": "Details here.",
    }
    # Only the GraphQL commit: no ref rewrite on the fast-forward path.
    assert [c[0] for c in api.calls] == ["POST"]
    # A whole-file payload must not go through argv.
    assert api.calls[0][3] is True
    # Later steps read the published head, which is GitHub's commit, not local HEAD.
    assert output.read_text() == "oid=f00d\n"


def test_reset_rebuilds_an_existing_branch_from_its_base_when_the_lease_holds(
    repo, monkeypatch
):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="stale1")
    monkeypatch.setattr(signed_commit, "gh_request", api)
    signed_commit.main(
        [
            "push",
            "--repo",
            REPO,
            "--branch",
            BRANCH,
            "--reset-to",
            base,
            "--lease",
            "stale1",
            "--cwd",
            str(repo),
        ]
    )
    methods = [(c[0], c[1]) for c in api.calls]
    assert methods == [
        ("GET", f"repos/{REPO}/git/ref/heads/{BRANCH}"),
        ("PATCH", f"repos/{REPO}/git/refs/heads/{BRANCH}"),
        ("POST", "graphql"),
    ]
    assert api.calls[1][2] == {"sha": base, "force": True}
    assert _graphql_input(api)["expectedHeadOid"] == base


def test_reset_creates_the_branch_when_the_lease_says_it_is_absent(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=None)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    signed_commit.main(
        [
            "push",
            "--repo",
            REPO,
            "--branch",
            BRANCH,
            "--reset-to",
            base,
            "--lease",
            "absent",
            "--cwd",
            str(repo),
        ]
    )
    assert api.calls[1][:3] == (
        "POST",
        f"repos/{REPO}/git/refs",
        {"ref": f"refs/heads/{BRANCH}", "sha": base},
    )


def test_reset_refuses_when_another_run_moved_the_branch(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="someone-elses")
    monkeypatch.setattr(signed_commit, "gh_request", api)
    with pytest.raises(SystemExit, match="moved"):
        signed_commit.main(
            [
                "push",
                "--repo",
                REPO,
                "--branch",
                BRANCH,
                "--reset-to",
                base,
                "--lease",
                "stale1",
                "--cwd",
                str(repo),
            ]
        )
    # Nothing written: no ref update, no commit.
    assert [c[0] for c in api.calls] == ["GET"]


def test_nothing_is_published_when_the_local_commit_changes_no_files(repo, monkeypatch):
    head = _git(repo, "rev-parse", "HEAD")
    api = FakeApi(branch_head=head)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    assert (
        signed_commit.main(
            [
                "push",
                "--repo",
                REPO,
                "--branch",
                BRANCH,
                "--expect-head",
                head,
                "--cwd",
                str(repo),
            ]
        )
        == 0
    )
    assert api.calls == []


def test_a_graphql_error_fails_the_step_instead_of_passing_silently(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(
        branch_head=base,
        commit_response=Response(
            200,
            {},
            {"data": None, "errors": [{"message": "Expected branch to point to abc"}]},
        ),
    )
    monkeypatch.setattr(signed_commit, "gh_request", api)
    with pytest.raises(SystemExit, match="Expected branch to point to abc"):
        signed_commit.main(
            [
                "push",
                "--repo",
                REPO,
                "--branch",
                BRANCH,
                "--expect-head",
                base,
                "--cwd",
                str(repo),
            ]
        )


def test_reset_without_a_lease_is_rejected(repo):
    with pytest.raises(SystemExit):
        signed_commit.main(
            [
                "push",
                "--repo",
                REPO,
                "--branch",
                BRANCH,
                "--reset-to",
                "abc",
                "--cwd",
                str(repo),
            ]
        )


def test_gh_request_sends_a_large_body_from_a_file_not_argv(monkeypatch):
    big = {"query": "q", "variables": {"blob": "x" * 200_000}}
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        assert "-d" not in cmd
        path = cmd[cmd.index("--data-binary") + 1].removeprefix("@")
        seen["path"] = path
        seen["body"] = json.loads(Path(path).read_text())
        return subprocess.CompletedProcess(cmd, 0, "HTTP/1.1 200 OK\n\n{}", "")

    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(_gh_refs, "run", fake_run)
    response = _gh_refs.gh_request("POST", "graphql", big, body_via_file=True)
    assert response.status == 200
    assert seen["body"] == big
    assert not Path(seen["path"]).exists()  # the temp file is removed afterwards

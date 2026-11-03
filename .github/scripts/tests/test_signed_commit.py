"""Tests for .github/scripts/signed_commit.py.

The org's signed-commits ruleset blocks a PR carrying an unsigned commit, and a
bot's ``git commit`` + ``git push`` is unsigned. These pin that the helper sends
the local commit's exact tree change through ``createCommitOnBranch`` (the path
GitHub signs), keeps each caller's overwrite guarantee, never leaves a branch
pointing at its bare base, refuses a change the API would silently lose a mode
from, waits out rate limits within a budget, and publishes nothing when nothing
changed.
"""

from __future__ import annotations

import base64
import json
import os
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
OID = "f00d"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "core.fileMode", "true")
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
    """Records gh_request calls and answers them from a small model of a repo."""

    def __init__(
        self,
        branch_head: str | None,
        commit_response: Response | None = None,
        script: list[Response] | None = None,
    ):
        self.branch_head = branch_head
        self.commit_response = commit_response or Response(
            200,
            {},
            {"data": {"createCommitOnBranch": {"commit": {"oid": OID, "url": "u"}}}},
        )
        # Responses served first, in order, before the model answers.
        self.script = list(script or [])
        self.calls: list[tuple[str, str, dict | None, bool]] = []
        self.tokens: list[str | None] = []

    def __call__(
        self, method, path, payload=None, *, sleep=None, body_via_file=False, token=None
    ):
        self.calls.append((method, path, payload, body_via_file))
        self.tokens.append(token)
        if self.script:
            return self.script.pop(0)
        if method == "GET":
            if self.branch_head is None:
                return Response(404, {}, {"message": "Not Found"})
            return Response(200, {}, {"object": {"sha": self.branch_head}})
        if path == "graphql":
            return self.commit_response
        if method == "DELETE":
            return Response(204, {}, None)
        return Response(200 if method == "PATCH" else 201, {}, {})

    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [(m, p, b) for m, p, b, _ in self.calls if m != "GET"]


def _graphql_input(api: FakeApi) -> dict:
    (call,) = [c for c in api.calls if c[1] == "graphql"]
    return call[2]["variables"]["input"]


def _push(repo: Path, *extra: str) -> int:
    return signed_commit.main(
        ["push", "--repo", REPO, "--branch", BRANCH, *extra, "--cwd", str(repo)]
    )


# ── what is published ─────────────────────────────────────────────────────


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


def _commit_paths(repo: Path, make) -> str:
    base = _git(repo, "rev-parse", "HEAD")
    make(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return base


def test_a_new_executable_is_refused_rather_than_published_as_a_plain_file(repo):
    def make(r: Path) -> None:
        script = r / "run.sh"
        script.write_text("#!/bin/sh\n")
        os.chmod(script, 0o755)

    base = _commit_paths(repo, make)
    with pytest.raises(SystemExit, match=r"run\.sh \(000000 -> 100755\)"):
        signed_commit.file_changes(base, "HEAD", repo)


def test_a_mode_only_change_is_refused(repo):
    base = _commit_paths(repo, lambda r: os.chmod(r / "pyproject.toml", 0o755))
    with pytest.raises(SystemExit, match=r"pyproject\.toml \(100644 -> 100755\)"):
        signed_commit.file_changes(base, "HEAD", repo)


def test_an_edit_to_an_existing_executable_is_refused(repo):
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add script")
    base = _commit_paths(repo, lambda r: script.write_text("#!/bin/sh\necho hi\n"))
    with pytest.raises(SystemExit, match=r"run\.sh \(100755 -> 100755\)"):
        signed_commit.file_changes(base, "HEAD", repo)


def test_a_new_symlink_is_refused(repo):
    base = _commit_paths(repo, lambda r: (r / "link").symlink_to("pyproject.toml"))
    with pytest.raises(SystemExit, match=r"link \(000000 -> 120000\)"):
        signed_commit.file_changes(base, "HEAD", repo)


def test_deleting_an_executable_is_publishable(repo):
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add script")
    base = _commit_paths(repo, lambda r: script.unlink())
    changes = signed_commit.file_changes(base, "HEAD", repo)
    assert changes == {"additions": [], "deletions": [{"path": "run.sh"}]}


# ── fast-forward ──────────────────────────────────────────────────────────


def test_expect_head_publishes_one_signed_commit_on_that_head(
    repo, monkeypatch, tmp_path
):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=base)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert _push(repo, "--expect-head", base) == 0
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
    assert output.read_text() == f"oid={OID}\n"


def test_expect_head_accepts_a_revision_and_resolves_it(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=base)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    _push(repo, "--expect-head", "HEAD~1")
    assert _graphql_input(api)["expectedHeadOid"] == base


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
        _push(repo, "--expect-head", base)


def test_nothing_is_published_when_the_local_commit_changes_no_files(
    repo, monkeypatch, tmp_path
):
    head = _git(repo, "rev-parse", "HEAD")
    api = FakeApi(branch_head=head)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert _push(repo, "--expect-head", head) == 0
    assert api.calls == []
    assert not output.exists()


# ── rebuild ───────────────────────────────────────────────────────────────


def _scratch(api: FakeApi) -> str:
    (create,) = [
        c for c in api.calls if c[0] == "POST" and c[1] == f"repos/{REPO}/git/refs"
    ][:1]
    return create[2]["ref"].removeprefix("refs/heads/")


def test_reset_rebuilds_an_existing_branch_from_its_base_when_the_lease_holds(
    repo, monkeypatch
):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="stale1")
    monkeypatch.setattr(signed_commit, "gh_request", api)
    _push(repo, "--reset-to", base, "--lease", "stale1")
    scratch = _scratch(api)
    assert scratch.startswith(f"{signed_commit.SCRATCH_PREFIX}/")
    assert [(c[0], c[1]) for c in api.calls] == [
        ("GET", f"repos/{REPO}/git/ref/heads/{BRANCH}"),
        ("POST", f"repos/{REPO}/git/refs"),
        ("POST", "graphql"),
        ("GET", f"repos/{REPO}/git/ref/heads/{BRANCH}"),
        ("PATCH", f"repos/{REPO}/git/refs/heads/{BRANCH}"),
        ("DELETE", f"repos/{REPO}/git/refs/heads/{scratch}"),
    ]
    assert api.calls[1][2] == {"ref": f"refs/heads/{scratch}", "sha": base}
    payload = _graphql_input(api)
    assert payload["branch"]["branchName"] == scratch
    assert payload["expectedHeadOid"] == base
    # The real branch moves straight onto the signed commit.
    assert api.calls[4][2] == {"sha": OID, "force": True}


def test_a_rebuild_never_points_the_real_branch_at_its_bare_base(repo, monkeypatch):
    # A PR whose head equals its base has no commits, and GitHub closes it.
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="stale1")
    monkeypatch.setattr(signed_commit, "gh_request", api)
    _push(repo, "--reset-to", base, "--lease", "stale1")
    real = [
        body
        for method, path, body in api.writes()
        if path.endswith(f"/heads/{BRANCH}")
        or (body or {}).get("ref") == f"refs/heads/{BRANCH}"
    ]
    assert real == [{"sha": OID, "force": True}]


def test_reset_creates_the_branch_when_the_lease_says_it_is_absent(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=None)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    _push(repo, "--reset-to", base, "--lease", "absent")
    assert api.calls[4][:3] == (
        "POST",
        f"repos/{REPO}/git/refs",
        {"ref": f"refs/heads/{BRANCH}", "sha": OID},
    )


def test_reset_refuses_when_another_run_moved_the_branch(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="someone-elses")
    monkeypatch.setattr(signed_commit, "gh_request", api)
    with pytest.raises(SystemExit, match="moved"):
        _push(repo, "--reset-to", base, "--lease", "stale1")
    # Nothing written: no scratch branch, no commit, no ref update.
    assert [c[0] for c in api.calls] == ["GET"]


def test_a_branch_moved_during_the_commit_is_not_overwritten_and_scratch_is_removed(
    repo, monkeypatch
):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="stale1")
    original = api.__call__

    def moving(method, path, payload=None, **kw):
        if path == "graphql":
            api.branch_head = "pushed-meanwhile"
        return original(method, path, payload, **kw)

    monkeypatch.setattr(signed_commit, "gh_request", moving)
    with pytest.raises(SystemExit, match="moved"):
        _push(repo, "--reset-to", base, "--lease", "stale1")
    methods = [c[0] for c in api.calls]
    assert "PATCH" not in methods
    assert methods[-1] == "DELETE"


def test_lease_any_overwrites_whatever_the_branch_holds(repo, monkeypatch):
    # The plain `git push --force` shape.
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head="anything")
    monkeypatch.setattr(signed_commit, "gh_request", api)
    _push(repo, "--reset-to", base, "--lease", "any")
    assert ("PATCH", f"repos/{REPO}/git/refs/heads/{BRANCH}") in [
        (c[0], c[1]) for c in api.calls
    ]


def test_lease_any_creates_a_missing_branch(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=None)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    _push(repo, "--reset-to", base, "--lease", "any")
    assert (
        "POST",
        f"repos/{REPO}/git/refs",
        {"ref": f"refs/heads/{BRANCH}", "sha": OID},
    ) in api.writes()


def test_reset_without_a_lease_is_rejected(repo):
    with pytest.raises(SystemExit):
        _push(repo, "--reset-to", "abc")


def test_publish_uses_the_token_it_is_given(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=None)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    signed_commit.publish(
        REPO, BRANCH, cwd=repo, reset_to=base, lease="absent", token="app-token"
    )
    assert api.tokens and set(api.tokens) == {"app-token"}


# ── rate limits ───────────────────────────────────────────────────────────

_LIMITED = Response(
    403, {"retry-after": "7"}, {"message": "You have exceeded a secondary rate limit"}
)


def test_a_rate_limited_write_is_retried_after_the_requested_wait(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=base, script=[_LIMITED])
    monkeypatch.setattr(signed_commit, "gh_request", api)
    slept: list[int] = []
    oid = signed_commit.publish(
        REPO, BRANCH, cwd=repo, expect_head=base, sleep=slept.append
    )
    assert oid == OID
    assert slept == [7]
    assert [c[1] for c in api.calls] == ["graphql", "graphql"]


def test_a_graphql_rate_limit_error_is_retried(repo, monkeypatch):
    base, _ = _commit_change(repo)
    limited = Response(200, {}, {"errors": [{"type": "RATE_LIMITED", "message": "x"}]})
    api = FakeApi(branch_head=base, script=[limited])
    monkeypatch.setattr(signed_commit, "gh_request", api)
    slept: list[int] = []
    signed_commit.publish(REPO, BRANCH, cwd=repo, expect_head=base, sleep=slept.append)
    assert slept == [signed_commit.DEFAULT_RATE_LIMIT_WAIT_SECONDS]


def test_rate_limit_waits_stop_at_the_budget(repo, monkeypatch):
    base, _ = _commit_change(repo)
    api = FakeApi(branch_head=base, script=[_LIMITED] * 1000)
    monkeypatch.setattr(signed_commit, "gh_request", api)
    slept: list[int] = []
    with pytest.raises(SystemExit, match="still rate-limited"):
        signed_commit.publish(
            REPO, BRANCH, cwd=repo, expect_head=base, sleep=slept.append
        )
    assert sum(slept) <= signed_commit.RATE_LIMIT_BUDGET_SECONDS
    assert sum(slept) + 7 > signed_commit.RATE_LIMIT_BUDGET_SECONDS


def test_a_permission_denial_is_not_retried(repo, monkeypatch):
    base, _ = _commit_change(repo)
    denied = Response(403, {}, {"message": "Resource not accessible by integration"})
    api = FakeApi(branch_head=None, script=[denied])
    monkeypatch.setattr(signed_commit, "gh_request", api)
    slept: list[int] = []
    with pytest.raises(SystemExit, match="Resource not accessible"):
        signed_commit.publish(
            REPO, BRANCH, cwd=repo, expect_head=base, sleep=slept.append
        )
    assert slept == [] and len(api.calls) == 1


# ── transport ─────────────────────────────────────────────────────────────


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


def test_gh_request_prefers_an_explicit_token_over_the_environment(monkeypatch):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["config"] = kwargs["input"]
        return subprocess.CompletedProcess(cmd, 0, "HTTP/1.1 200 OK\n\n{}", "")

    monkeypatch.setenv("GH_TOKEN", "env-token")
    monkeypatch.setattr(_gh_refs, "run", fake_run)
    _gh_refs.gh_request("GET", "x", token="given-token")
    assert "given-token" in seen["config"] and "env-token" not in seen["config"]

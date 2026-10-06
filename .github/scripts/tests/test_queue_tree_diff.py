"""Tests for .github/scripts/queue_tree_diff.py.

The comparison runs against real git repositories in ``tmp_path``: an origin
holding a PR head and a queue commit, and a depth-1 clone of the queue commit,
the same shape the workflow's checkout leaves. The fail-safe paths stub the
module's ``git`` seam, because the point there is what happens when git does
not answer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from queue_tree_diff import decide, is_image_input, main, parse_pr_head  # noqa: E402

PR_SHA = "a" * 40


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "ci")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    _commit(repo, {"app/main.py": "print(1)\n", "uv.lock": "v1\n"}, "base")
    return repo


def _queue_entry(
    origin: Path, tmp_path: Path, base_change: dict[str, str]
) -> tuple[str, str, Path]:
    """PR head on a branch; the queue commit = PR head plus ``base_change``.

    Returns (pr_sha, queue_sha, clone) with ``clone`` a depth-1 clone of the
    queue commit only, so the PR head has to be fetched.
    """
    _git(origin, "checkout", "-q", "-b", "pr")
    pr_sha = _commit(origin, {"app/feature.py": "x = 1\n"}, "pr")
    queue_sha = _commit(origin, base_change, "base moved") if base_change else pr_sha
    _git(origin, "branch", "-q", "queue", queue_sha)
    _git(origin, "reset", "-q", "--hard", pr_sha)
    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth=1",
            "--branch",
            "queue",
            origin.as_uri(),
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    return pr_sha, queue_sha, clone


def _ref(pr_sha: str) -> str:
    return f"refs/heads/gh-readonly-queue/main/pr-42-{pr_sha}"


def _decide_in(
    clone: Path, pr_sha: str, queue_sha: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(clone)
    return decide("merge_group", _ref(pr_sha), queue_sha)


# ── against real repositories ────────────────────────────────────────────────


def test_identical_tree_skips_everything(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue commit is a different commit with the PR head's exact tree —
    a squash of an up-to-date PR — so both answers say "nothing to re-check"."""
    _git(origin, "checkout", "-q", "-b", "pr")
    pr_sha = _commit(origin, {"app/feature.py": "x = 1\n"}, "pr")
    tree = _git(origin, "rev-parse", f"{pr_sha}^{{tree}}")
    squash = _git(origin, "commit-tree", tree, "-p", "main", "-m", "squash")
    _git(origin, "branch", "-q", "queue", squash)
    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth=1",
            "--branch",
            "queue",
            origin.as_uri(),
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    assert squash != pr_sha
    decision = _decide_in(clone, pr_sha, squash, monkeypatch)
    assert decision.identical is True
    assert decision.image_changed is False


def test_source_only_base_change_reruns_tree_checks_not_the_scan(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr_sha, queue_sha, clone = _queue_entry(
        origin, tmp_path, {"app/other.py": "y = 2\n"}
    )
    decision = _decide_in(clone, pr_sha, queue_sha, monkeypatch)
    assert decision.identical is False
    assert decision.image_changed is False
    assert "none is an image input" in decision.reason


def test_lock_change_from_the_base_reruns_the_scan(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr_sha, queue_sha, clone = _queue_entry(
        origin, tmp_path, {"uv.lock": "v2\n", "app/other.py": "y = 2\n"}
    )
    decision = _decide_in(clone, pr_sha, queue_sha, monkeypatch)
    assert decision.identical is False
    assert decision.image_changed is True
    assert "uv.lock" in decision.reason


def test_a_deleted_image_input_counts(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-renames` so a removed or renamed Dockerfile is listed by its old
    path too, not folded into a rename the matcher never sees."""
    _commit(origin, {"Dockerfile": "FROM scratch\n"}, "dockerfile")
    _git(origin, "checkout", "-q", "-b", "pr")
    pr_sha = _commit(origin, {"app/feature.py": "x = 1\n"}, "pr")
    (origin / "docker").mkdir()
    _git(origin, "mv", "Dockerfile", "docker/Image")
    _git(origin, "commit", "-q", "-m", "move")
    queue_sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "branch", "-q", "queue", queue_sha)
    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth=1",
            "--branch",
            "queue",
            origin.as_uri(),
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    decision = _decide_in(clone, pr_sha, queue_sha, monkeypatch)
    assert decision.image_changed is True


def test_unfetchable_pr_head_fails_safe(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, queue_sha, clone = _queue_entry(origin, tmp_path, {"app/other.py": "y = 2\n"})
    decision = _decide_in(clone, "b" * 40, queue_sha, monkeypatch)
    assert decision.identical is False
    assert decision.image_changed is True
    assert "git failed" in decision.reason


# ── fail-safe paths, git stubbed ─────────────────────────────────────────────


def _never_called(args: list[str]) -> str:
    raise AssertionError(f"git must not run here: {args}")


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch", ""])
def test_outside_the_queue_everything_runs(event: str) -> None:
    decision = decide(event, _ref(PR_SHA), "c" * 40, git=_never_called)
    assert (decision.identical, decision.image_changed) == (False, True)


@pytest.mark.parametrize(
    "head_ref",
    [
        "",
        "refs/heads/main",
        "refs/heads/gh-readonly-queue/main/pr-42-abc123",
        f"refs/heads/gh-readonly-queue/main/pr-42-{PR_SHA}; rm -rf /",
        f"refs/heads/gh-readonly-queue/main/pr--{PR_SHA}",
    ],
)
def test_unparseable_queue_ref_fails_safe(head_ref: str) -> None:
    decision = decide("merge_group", head_ref, "c" * 40, git=_never_called)
    assert (decision.identical, decision.image_changed) == (False, True)


def test_bad_queue_sha_fails_safe() -> None:
    decision = decide("merge_group", _ref(PR_SHA), "HEAD", git=_never_called)
    assert (decision.identical, decision.image_changed) == (False, True)


def test_missing_git_fails_safe() -> None:
    def no_git(args: list[str]) -> str:
        raise FileNotFoundError("git")

    decision = decide("merge_group", _ref(PR_SHA), "c" * 40, git=no_git)
    assert (decision.identical, decision.image_changed) == (False, True)


def test_differing_trees_with_empty_diff_fails_safe() -> None:
    answers = iter(["", "tree-a", "tree-b", ""])
    decision = decide(
        "merge_group", _ref(PR_SHA), "c" * 40, git=lambda _a: next(answers)
    )
    assert (decision.identical, decision.image_changed) == (False, True)


def test_parse_pr_head_reads_nested_base_branches() -> None:
    assert (
        parse_pr_head(f"refs/heads/gh-readonly-queue/release/v3/pr-7-{PR_SHA}")
        == PR_SHA
    )


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile",
        "docker/Dockerfile.worker",
        "build/app.dockerfile",
        ".dockerignore",
        "uv.lock",
        "packages/sub/pyproject.toml",
        "requirements-dev.txt",
        "frontend/package-lock.json",
        "vendor/parser.jar",
        "scripts/install-driver.sh",
        "atlan.yaml",
        ".security/allowlist.json",
    ],
)
def test_image_inputs(path: str) -> None:
    assert is_image_input(path)


@pytest.mark.parametrize(
    "path",
    [
        "app/main.py",
        "tests/unit/test_x.py",
        "README.md",
        ".github/workflows/tests.yaml",
        "docs/x.md",
    ],
)
def test_not_image_inputs(path: str) -> None:
    assert not is_image_input(path)


def test_main_writes_both_outputs(tmp_path: Path) -> None:
    output = tmp_path / "out"
    rc = main(
        {"EVENT_NAME": "pull_request", "GITHUB_OUTPUT": str(output)},
        git=_never_called,
    )
    assert rc == 0
    assert output.read_text() == "identical=false\nimage_changed=true\n"

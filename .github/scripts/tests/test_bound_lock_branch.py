"""Tests for .github/scripts/bound_lock_branch.py.

Pure-unit: the bound itself is stubbed (it has its own suite in
test_renovate_uv_lock_bounded.py), and uv is never invoked. What needs cover here
is the orchestration around it, because every one of its failure modes is silent:
a project skipped, an exempt set that lost a name, a partial commit that merges an
unbounded lock, or a requirements.txt left describing the lock we just replaced.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import bound_lock_branch as orchestrator
import renovate_approval_conditions as approval

WORKFLOW = (
    Path(__file__).parent.parent.parent / "workflows" / "renovate-lock-cooldown.yaml"
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": __import__("os").environ.get("PATH", ""),
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, env=GIT_ENV, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like application-sdk: two uv projects and a requirements.txt."""
    (tmp_path / "uv.lock").write_text("root lock\n")
    (tmp_path / "requirements.txt").write_text("stale==1.0.0\n")
    sub = tmp_path / "packages" / "conformance"
    sub.mkdir(parents=True)
    (sub / "uv.lock").write_text("sub lock\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


@pytest.fixture
def in_repo(repo: Path, monkeypatch) -> Path:
    monkeypatch.chdir(repo)
    return repo


def head_files(repo: Path) -> set[str]:
    return set(git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())


def head_subject(repo: Path) -> str:
    return git(repo, "log", "-1", "--format=%s").stdout.strip()


class TestWorkflowGuards:
    """The `if:` on the job, which is two guards doing two unrelated jobs.

    Both are asserted here rather than trusted, because both fail silently. A
    skipped job is not red anywhere: an actor list that drifts out of step with
    the identity actually pushing would stop bounding the lock and announce
    nothing, which is the shape of failure FND-367 already paid for once.
    """

    @property
    def job(self) -> dict:
        workflow = yaml.safe_load(WORKFLOW.read_text())
        return workflow["jobs"]["bound"]

    def test_the_actor_list_matches_the_approval_gates_renovate_identities(self):
        """One list of Renovate identities, not two.

        The gate accepts these as PR authors; this workflow accepts them as
        pushers. They describe the same fact — "a Renovate is driving this" — so a
        change to one that misses the other leaves the lane running for a PR it
        will not approve, or approving a PR it did not bound.
        """
        declared = re.findall(r'"([a-z0-9-]+\[bot\])"', self.job["if"])
        assert sorted(declared) == sorted(approval.RENOVATE_AUTHORS), (
            "the workflow's actor allowlist has drifted from "
            f"RENOVATE_AUTHORS ({approval.RENOVATE_AUTHORS})"
        )

    def test_the_condition_folds_to_a_single_line(self):
        # The `if:` is a folded scalar spanning two lines. A continuation line
        # indented deeper than the block keeps its newline rather than folding to
        # a space, leaving a literal line break inside the `${{ }}` — which is
        # not something YAML validation or a lint pass flags.
        assert "\n" not in self.job["if"]

    def test_the_guard_reads_the_actor_and_not_something_spoofable(self):
        # `github.actor` is set by GitHub from the push. Deriving the identity
        # from anything inside the pushed content — a commit author, a trailer —
        # would let the push assert who made it.
        assert "github.actor" in self.job["if"]
        assert "head_commit.author" not in self.job["if"]

    def test_re_entrancy_is_guarded_by_the_commit_subject_we_actually_write(self):
        # The actor list admits atlan-app-fleet[bot], which is the identity our
        # own push arrives as, so the actor check cannot be what stops the loop.
        # This is — and it only works while it matches the driver's message.
        assert orchestrator.COMMIT_MESSAGE.startswith(
            "chore(deps): bound the refreshed locks"
        )
        assert "chore(deps): bound the refreshed locks" in self.job["if"]

    def test_the_lane_is_scoped_to_the_lock_refresh_branch(self):
        # Widening this to `renovate/**` would put the bound on the single-package
        # lanes, where a deliberately chosen first-party version has no business
        # being delayed.
        workflow = yaml.safe_load(WORKFLOW.read_text())
        # YAML 1.1 resolves a bare `on` to the boolean True, so that — not the
        # string — is the key the loader produces for a workflow's trigger block.
        triggers = workflow[True]
        assert triggers["push"]["branches"] == ["renovate/lock-file-maintenance"]


class TestProjects:
    """The declared project set, guarded because both entries are load-bearing."""

    def test_both_uv_projects_are_covered(self):
        assert [p.directory for p in orchestrator.PROJECTS] == [
            ".",
            "packages/conformance",
        ]

    def test_the_conformance_project_exempts_the_sdk_and_pyatlan(self):
        """Not interchangeable with the root's exempt set, and not shrinkable.

        packages/conformance resolves atlan-application-sdk from PyPI, and the SDK
        requires pyatlan>=10. Exempting the SDK without pyatlan does not fail — a
        bounded resolve that cannot reach pyatlan 10 quietly backtracks to an
        older SDK instead, which is how this went unnoticed the first time.
        """
        by_dir = {p.directory: set(p.exempt) for p in orchestrator.PROJECTS}
        assert by_dir["packages/conformance"] == {"atlan-application-sdk", "pyatlan"}
        # The root project consumes neither of its own packages from PyPI:
        # atlan-application-sdk IS this project, and the conformance package is
        # path-sourced via [tool.uv.sources].
        assert by_dir["."] == {"pyatlan"}


class TestBoundProject:
    def test_each_project_gets_its_own_exempt_flags_and_the_shared_baseline(
        self, monkeypatch, tmp_path
    ):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            orchestrator.bounded, "main", lambda argv: calls.append(argv) or 0
        )
        for project in orchestrator.PROJECTS:
            orchestrator.bound_project(project, "P7D", "origin/main", tmp_path)

        assert len(calls) == 2
        for argv, project in zip(calls, orchestrator.PROJECTS):
            assert argv[argv.index("--window") + 1] == "P7D"
            assert argv[argv.index("--baseline-ref") + 1] == "origin/main"
            assert argv[argv.index("--project-dir") + 1] == str(
                tmp_path / project.directory
            )
            exempt = [argv[i + 1] for i, a in enumerate(argv) if a == "--exempt"]
            assert exempt == list(project.exempt)


class TestMain:
    """End to end with the bound stubbed. The invariant under test throughout is
    that nothing reaches a commit unless every project was bounded successfully."""

    def _stub_bound(self, monkeypatch, *, rewrite: bool = True):
        """Stand in for the driver. `rewrite=False` models an already-bound branch."""

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            if rewrite:
                (directory / "uv.lock").write_text("bounded\n")
            return 0

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)

    def test_one_commit_carries_both_locks_and_the_requirements_export(
        self, monkeypatch, in_repo
    ):
        """One commit, not three: each push re-fires the PR's whole check suite."""
        self._stub_bound(monkeypatch)
        monkeypatch.setattr(
            orchestrator,
            "export_requirements",
            lambda root: (root / "requirements.txt").write_text("bounded==2.0.0\n"),
        )

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert head_files(in_repo) == {
            "uv.lock",
            "packages/conformance/uv.lock",
            "requirements.txt",
        }
        assert head_subject(in_repo) == orchestrator.COMMIT_MESSAGE

    def test_a_failed_bound_commits_nothing_at_all(self, monkeypatch, in_repo):
        """Fail-closed, and specifically not fail-partial.

        The second project failing must not leave the first one's bounded lock
        committed on its own — a half-bounded branch still auto-merges, and it
        would look like the control worked.
        """

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            (directory / "uv.lock").write_text("bounded\n")
            return 0 if directory.name != "conformance" else 1

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)
        exported: list[Path] = []
        monkeypatch.setattr(
            orchestrator, "export_requirements", lambda root: exported.append(root)
        )

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 1
        assert head_subject(in_repo) == "base"
        assert exported == [], "the export must not run against a half-bound tree"

    def test_a_failed_requirements_export_commits_nothing(self, monkeypatch, in_repo):
        # requirements.txt is what the Dockerfiles and downstream installs read.
        # Committing the bounded locks while it still describes the pre-bound
        # resolve would ship the bound and the thing it replaced in one PR.
        self._stub_bound(monkeypatch)

        def boom(root: Path) -> None:
            raise RuntimeError("uv export failed")

        monkeypatch.setattr(orchestrator, "export_requirements", boom)
        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 1
        assert head_subject(in_repo) == "base"

    def test_an_already_bounded_branch_produces_no_commit(self, monkeypatch, in_repo):
        """Idempotence, which is what makes the workflow's own push self-limiting.

        The re-entrancy guard in the workflow saves the second resolve; this is
        the backstop that stops a loop if that guard is ever removed.
        """
        self._stub_bound(monkeypatch, rewrite=False)
        monkeypatch.setattr(orchestrator, "export_requirements", lambda root: None)

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert head_subject(in_repo) == "base"

    def test_only_the_declared_paths_are_staged(self, monkeypatch, in_repo):
        # The branch auto-merges, so anything incidental in the working tree — a
        # uv cache, a stray artefact — must not be able to ride along.
        self._stub_bound(monkeypatch)
        monkeypatch.setattr(orchestrator, "export_requirements", lambda root: None)
        (in_repo / "SHOULD-NOT-BE-COMMITTED").write_text("x\n")

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert "SHOULD-NOT-BE-COMMITTED" not in head_files(in_repo)


class TestExportRequirements:
    def test_export_is_frozen_so_it_cannot_re_resolve(self):
        """`--frozen` makes this a projection of the bound lock, not a resolve.

        Without it, `uv export` may update the lock — re-introducing precisely the
        versions the bound just excluded, into the file downstream installs read.
        And not `--all-extras`, which would widen what gets pinned.
        """
        assert orchestrator.REQUIREMENTS_EXPORT == [
            "uv",
            "export",
            "--no-hashes",
            "--frozen",
        ]

    def test_a_failing_export_raises_rather_than_leaving_a_stale_file(
        self, monkeypatch, repo
    ):
        monkeypatch.setattr(
            orchestrator.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "no solution"),
        )
        with pytest.raises(RuntimeError, match="requirements.txt"):
            orchestrator.export_requirements(repo)
        assert (repo / "requirements.txt").read_text() == "stale==1.0.0\n"

    def test_a_repo_without_requirements_txt_is_a_clean_no_op(self, monkeypatch, repo):
        (repo / "requirements.txt").unlink()
        monkeypatch.setattr(
            orchestrator.subprocess,
            "run",
            lambda *a, **k: pytest.fail("uv export must not run with no target"),
        )
        orchestrator.export_requirements(repo)
        assert not (repo / "requirements.txt").exists()

    def test_a_successful_export_replaces_the_file(self, monkeypatch, repo):
        monkeypatch.setattr(
            orchestrator.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, "bounded==2.0.0\n", ""
            ),
        )
        orchestrator.export_requirements(repo)
        assert (repo / "requirements.txt").read_text() == "bounded==2.0.0\n"

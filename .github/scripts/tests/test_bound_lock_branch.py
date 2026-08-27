"""Tests for .github/scripts/bound_lock_branch.py.

Pure-unit: the bound itself is stubbed (it has its own suite in
test_renovate_uv_lock_bounded.py), and uv is never invoked. What needs cover here
is the orchestration around it, because every one of its failure modes is silent:
a project skipped, an exempt set that lost a name, or a partial commit that merges
an unbounded lock.
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
    """A repo shaped like application-sdk: two uv projects and one npm project.
    All three files the refresh lane rewrites."""
    (tmp_path / "uv.lock").write_text("root lock\n")
    sub = tmp_path / "packages" / "conformance"
    sub.mkdir(parents=True)
    (sub / "uv.lock").write_text("sub lock\n")
    npm_project = tmp_path / orchestrator.NPM_PROJECT
    npm_project.mkdir(parents=True, exist_ok=True)
    (npm_project / "package.json").write_text('{"name": "remediation"}\n')
    (npm_project / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
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

    def test_the_bound_step_pins_the_version_parser_it_needs(self):
        """The runner image is not a promise.

        The driver's rollback gate needs PEP 440 parsing, and `python3` on a bare
        runner is not guaranteed to have `packaging` importable. Without it the
        gate reports every upgrade as a regression, so the dependency is declared
        at the call site rather than assumed — pinned, and `--no-project` so the
        bound never waits on a full dev sync.
        """
        steps = self.job["steps"]
        bound_step = next(
            s for s in steps if s.get("name") == "Bound the refreshed locks"
        )
        run = bound_step["run"]
        assert "bound_lock_branch.py" in run
        assert "--with 'packaging==" in run, run
        assert "--no-project" in run, run

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


class TestBoundNpm:
    """The fourth file the lane rewrites, bounded by a different mechanism."""

    def test_the_npm_project_is_the_one_under_packages_conformance(self):
        # Two levels of `conformance`, which is easy to get wrong by one: the uv
        # project is packages/conformance, the npm project is one deeper.
        assert orchestrator.NPM_PROJECT == "packages/conformance/conformance"

    def test_the_npm_driver_gets_the_same_window_and_baseline_as_the_uv_bound(
        self, monkeypatch, tmp_path
    ):
        """A second window in one workflow would need its own justification, and a
        baseline other than the base branch is what makes "never roll back what
        main ships" structural rather than a claim."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            orchestrator.npm_bounded, "main", lambda argv: calls.append(argv) or 0
        )
        orchestrator.bound_npm("P3D", "origin/main", tmp_path)

        (argv,) = calls
        assert argv[argv.index("--window") + 1] == "P3D"
        assert argv[argv.index("--baseline-ref") + 1] == "origin/main"
        assert argv[argv.index("--project-dir") + 1] == str(
            tmp_path / orchestrator.NPM_PROJECT
        )
        # The npm bound has no exempt set: exemptions exist to let a first-party
        # package move ahead of the window, and none of these three dev-only
        # devDependencies is Atlan-published.
        assert "--exempt" not in argv


class TestMain:
    """End to end with the bound stubbed. The invariant under test throughout is
    that nothing reaches a commit unless every project was bounded successfully."""

    def _stub_bound(self, monkeypatch, *, rewrite: bool = True, npm_code: int = 0):
        """Stand in for both drivers. `rewrite=False` models an already-bound branch."""

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            if rewrite:
                (directory / "uv.lock").write_text("bounded\n")
            return 0

        def fake_npm_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            if rewrite and npm_code == 0:
                (directory / "package-lock.json").write_text('{"bounded": true}\n')
            return npm_code

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)
        monkeypatch.setattr(orchestrator.npm_bounded, "main", fake_npm_main)

    def test_one_commit_carries_every_lock(self, monkeypatch, in_repo):
        """One commit, not three: each push re-fires the PR's whole check suite."""
        self._stub_bound(monkeypatch)

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert head_files(in_repo) == {
            "uv.lock",
            "packages/conformance/uv.lock",
            f"{orchestrator.NPM_PROJECT}/package-lock.json",
        }
        assert head_subject(in_repo) == orchestrator.COMMIT_MESSAGE

    def test_a_failed_npm_bound_commits_nothing_at_all(self, monkeypatch, in_repo):
        """Same fail-closed-not-fail-partial rule the uv projects get.

        A half-bounded branch still auto-merges and still looks like the control
        worked. The npm driver reserves a non-zero exit for the cases where it
        could not establish a safe outcome at all — a declined bound is exit 0,
        precisely so an ordinary decline does not throw away the uv bounds that
        did apply.
        """
        self._stub_bound(monkeypatch, npm_code=1)

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 1
        assert head_subject(in_repo) == "base"

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

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 1
        assert head_subject(in_repo) == "base"

    def test_an_already_bounded_branch_produces_no_commit(self, monkeypatch, in_repo):
        """Idempotence, which is what makes the workflow's own push self-limiting.

        The re-entrancy guard in the workflow saves the second resolve; this is
        the backstop that stops a loop if that guard is ever removed.
        """
        self._stub_bound(monkeypatch, rewrite=False)

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert head_subject(in_repo) == "base"

    def test_only_the_declared_paths_are_staged(self, monkeypatch, in_repo):
        # The branch auto-merges, so anything incidental in the working tree — a
        # uv cache, a stray artefact — must not be able to ride along.
        self._stub_bound(monkeypatch)
        (in_repo / "SHOULD-NOT-BE-COMMITTED").write_text("x\n")

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert "SHOULD-NOT-BE-COMMITTED" not in head_files(in_repo)


class TestOwnsItsCommit:
    """This script pushes its own commit, and the driver has to be told so.

    Without `--caller-owns-commit` the driver treats "the bound admits nothing" as
    the postUpgradeTasks substitution risk and exits non-zero, so this script
    pushes nothing and Renovate's unbounded commit stays on the branch. That is
    how #3290 merged a 4-hour-old boto3.
    """

    def test_the_driver_is_told_that_this_script_owns_the_commit(self, monkeypatch):
        seen: list[list[str]] = []

        def fake_main(argv):
            seen.append(argv)
            return 0

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)
        project = orchestrator.PROJECTS[0]
        assert orchestrator.bound_project(project, "P3D", "origin/main", Path(".")) == 0
        assert "--caller-owns-commit" in seen[0], seen[0]

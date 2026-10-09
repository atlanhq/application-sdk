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
import tomllib
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


def uv_lock(pyatlan: str | None = "11.4.0", marker: str = "base") -> str:
    """A minimal uv.lock. `marker` stands in for whatever else a bound rewrites."""
    text = (
        f'version = 1\n# {marker}\n\n[[package]]\nname = "boto3"\nversion = "1.0.0"\n'
    )
    if pyatlan is not None:
        text += f'\n[[package]]\nname = "pyatlan"\nversion = "{pyatlan}"\n'
    return text


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like application-sdk: two uv projects, one npm project and
    the relationship table. Every file the refresh lane may rewrite."""
    (tmp_path / "uv.lock").write_text(uv_lock(marker="root"))
    sub = tmp_path / "packages" / "conformance"
    sub.mkdir(parents=True)
    (sub / "uv.lock").write_text(uv_lock(marker="sub"))
    npm_project = tmp_path / orchestrator.NPM_PROJECT
    npm_project.mkdir(parents=True, exist_ok=True)
    (npm_project / "package.json").write_text('{"name": "remediation"}\n')
    (npm_project / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    table = tmp_path / orchestrator.RELATIONSHIP_DIRECTIONS
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text('{"table": "11.4.0"}\n')
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

    def test_the_conformance_project_holds_the_sdk_and_exempts_pyatlan(self):
        """packages/conformance resolves atlan-application-sdk from PyPI. The
        lock refresh must not move it (FND-3481): the atlan framework
        dependencies lane owns it, and both lanes rewriting the same entries is
        what left one PR in conflict whichever merged first."""
        exempt = {p.directory: set(p.exempt) for p in orchestrator.PROJECTS}
        hold = {p.directory: set(p.hold) for p in orchestrator.PROJECTS}
        assert hold["packages/conformance"] == {"atlan-application-sdk"}
        assert exempt["packages/conformance"] == {"pyatlan"}
        # The root project consumes neither of its own packages from PyPI:
        # atlan-application-sdk IS this project, and the conformance package is
        # path-sourced via [tool.uv.sources].
        assert exempt["."] == {"pyatlan"}
        assert hold["."] == set()


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
            hold = [argv[i + 1] for i, a in enumerate(argv) if a == "--hold"]
            assert hold == list(project.hold)


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
        """Stand in for both drivers. `rewrite=False` models an already-bound branch.

        The rewrite leaves pyatlan where it was, so the regeneration is stubbed
        to fail loudly: none of these tests should reach it.
        """

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            if rewrite:
                (directory / "uv.lock").write_text(uv_lock(marker="bounded"))
            return 0

        def fake_npm_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            if rewrite and npm_code == 0:
                (directory / "package-lock.json").write_text('{"bounded": true}\n')
            return npm_code

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)
        monkeypatch.setattr(orchestrator.npm_bounded, "main", fake_npm_main)
        monkeypatch.setattr(
            orchestrator,
            "regenerate_relationship_directions",
            lambda root: pytest.fail("pyatlan did not move; nothing to regenerate"),
        )

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
            (directory / "uv.lock").write_text(uv_lock(marker="bounded"))
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


class TestPyatlanVersion:
    def test_reads_the_locked_version(self):
        assert orchestrator.pyatlan_version(uv_lock("11.4.1")) == "11.4.1"

    def test_absent_is_none_not_an_error(self):
        assert orchestrator.pyatlan_version(uv_lock(None)) is None

    def test_an_unparseable_lock_raises_rather_than_reading_as_unchanged(self):
        # Answering "no pyatlan" for a corrupt lock would compare equal to another
        # corrupt read and skip the regeneration without a word.
        with pytest.raises(tomllib.TOMLDecodeError):
            orchestrator.pyatlan_version("not = [toml\n")


class TestRegeneration:
    """The relationship table follows the BOUNDED lock's pyatlan, in the same
    commit, and a failed rebuild commits nothing."""

    def _stub(self, monkeypatch, *, pyatlan: str, regen_code: int = 0):
        calls: list[Path] = []

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            (directory / "uv.lock").write_text(uv_lock(pyatlan, marker="bounded"))
            return 0

        def fake_regen(root: Path) -> int:
            calls.append(root)
            if regen_code == 0:
                (root / orchestrator.RELATIONSHIP_DIRECTIONS).write_text(
                    f'{{"table": "{pyatlan}"}}\n'
                )
            return regen_code

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)
        monkeypatch.setattr(orchestrator.npm_bounded, "main", lambda argv: 0)
        monkeypatch.setattr(
            orchestrator, "regenerate_relationship_directions", fake_regen
        )
        return calls

    def test_a_pyatlan_move_regenerates_and_commits_the_table(
        self, monkeypatch, in_repo
    ):
        calls = self._stub(monkeypatch, pyatlan="11.4.1")

        assert orchestrator.main(["--window", "P3D", "--baseline-ref", "HEAD"]) == 0
        assert calls == [in_repo]
        assert orchestrator.RELATIONSHIP_DIRECTIONS in head_files(in_repo)
        assert head_subject(in_repo) == orchestrator.COMMIT_MESSAGE

    def test_an_unmoved_pyatlan_does_not_regenerate(self, monkeypatch, in_repo):
        calls = self._stub(monkeypatch, pyatlan="11.4.0")

        assert orchestrator.main(["--window", "P3D", "--baseline-ref", "HEAD"]) == 0
        assert calls == []

    def test_a_failed_regeneration_commits_nothing(self, monkeypatch, in_repo):
        # Locks without their table are the red PR this exists to prevent.
        self._stub(monkeypatch, pyatlan="11.4.1", regen_code=1)

        assert orchestrator.main(["--window", "P3D", "--baseline-ref", "HEAD"]) == 1
        assert head_subject(in_repo) == "base"

    def test_the_regeneration_never_relocks(self, monkeypatch, tmp_path):
        """`uv run` without --frozen may rewrite the lock the bound just wrote."""
        seen: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
        orchestrator.regenerate_relationship_directions(tmp_path)

        (cmd,) = seen
        assert cmd[:3] == ["uv", "run", "--frozen"]
        assert cmd[-1] == "gen-relationship-directions"


class TestTwoJobHandoff:
    """`--no-commit` in the job without the token, `--apply-from` in the one with."""

    def test_no_commit_leaves_the_outputs_uncommitted(self, monkeypatch, in_repo):
        TestMain()._stub_bound(monkeypatch)

        argv = ["--window", "P3D", "--baseline-ref", "HEAD", "--no-commit"]
        assert orchestrator.main(argv) == 0
        assert head_subject(in_repo) == "base"
        assert "bounded" in (in_repo / "uv.lock").read_text()

    def test_apply_from_commits_the_artifact(self, in_repo, tmp_path_factory):
        artifact = tmp_path_factory.mktemp("artifact")
        for path in orchestrator.OUTPUT_PATHS:
            (artifact / path).parent.mkdir(parents=True, exist_ok=True)
            (artifact / path).write_text(f"from artifact: {path}\n")

        assert orchestrator.main(["--apply-from", str(artifact)]) == 0
        assert head_files(in_repo) == set(orchestrator.OUTPUT_PATHS)
        assert head_subject(in_repo) == orchestrator.COMMIT_MESSAGE

    @staticmethod
    def _unchanged_artifact(in_repo: Path, tmp_path_factory) -> Path:
        """Every output present, byte-identical to the checkout."""
        artifact = tmp_path_factory.mktemp("artifact")
        for path in orchestrator.OUTPUT_PATHS:
            (artifact / path).parent.mkdir(parents=True, exist_ok=True)
            (artifact / path).write_bytes((in_repo / path).read_bytes())
        return artifact

    def test_apply_from_copies_only_the_declared_paths(self, in_repo, tmp_path_factory):
        # The publishing job holds the push token; the artifact must not be able
        # to widen what it commits.
        artifact = self._unchanged_artifact(in_repo, tmp_path_factory)
        (artifact / "uv.lock").write_text(uv_lock(marker="bounded"))
        (artifact / ".github" / "workflows").mkdir(parents=True)
        (artifact / ".github" / "workflows" / "evil.yaml").write_text("x\n")

        assert orchestrator.main(["--apply-from", str(artifact)]) == 0
        assert head_files(in_repo) == {"uv.lock"}
        assert not (in_repo / ".github" / "workflows" / "evil.yaml").exists()

    @pytest.mark.parametrize("missing", orchestrator.OUTPUT_PATHS)
    def test_apply_from_rejects_an_incomplete_artifact(
        self, in_repo, tmp_path_factory, missing
    ):
        """A missing output is a broken handoff, not "unchanged".

        Skipping it would leave Renovate's unbounded lock on the branch behind a
        green publish. Nothing is copied either: the check runs before any copy.
        """
        artifact = self._unchanged_artifact(in_repo, tmp_path_factory)
        (artifact / "uv.lock").write_text(uv_lock(marker="bounded"))
        if missing == "uv.lock":
            (artifact / orchestrator.PYATLAN_LOCK).write_text(uv_lock(marker="x"))
        (artifact / missing).unlink()

        with pytest.raises(FileNotFoundError, match=missing):
            orchestrator.main(["--apply-from", str(artifact)])
        assert head_subject(in_repo) == "base"
        assert git(in_repo, "status", "--porcelain").stdout == ""

    def test_apply_from_refuses_a_symlink(self, in_repo, tmp_path_factory):
        artifact = self._unchanged_artifact(in_repo, tmp_path_factory)
        outside = tmp_path_factory.mktemp("elsewhere") / "runner-file"
        outside.write_text("not ours\n")
        (artifact / "uv.lock").unlink()
        (artifact / "uv.lock").symlink_to(outside)

        with pytest.raises(ValueError, match="symlink"):
            orchestrator.main(["--apply-from", str(artifact)])
        assert head_subject(in_repo) == "base"

    def test_an_unchanged_artifact_commits_nothing(self, in_repo, tmp_path_factory):
        artifact = tmp_path_factory.mktemp("artifact")
        for path in orchestrator.OUTPUT_PATHS:
            (artifact / path).parent.mkdir(parents=True, exist_ok=True)
            (artifact / path).write_bytes((in_repo / path).read_bytes())

        assert orchestrator.main(["--apply-from", str(artifact)]) == 0
        assert head_subject(in_repo) == "base"

    def test_bounding_still_requires_window_and_baseline(self):
        with pytest.raises(SystemExit):
            orchestrator.main(["--window", "P3D"])

    def test_the_modes_are_exclusive(self, tmp_path):
        with pytest.raises(SystemExit):
            orchestrator.main(["--no-commit", "--apply-from", str(tmp_path)])


class TestJobSplit:
    """The point of the split is where the App token lives, so assert it
    structurally rather than trust the comments."""

    @property
    def jobs(self) -> dict:
        return yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def _step(self, job: str, uses: str) -> dict:
        return next(s for s in self.jobs[job]["steps"] if uses in s.get("uses", ""))

    def test_the_bound_job_never_sees_a_secret_or_a_write_permission(self):
        bound = self.jobs["bound"]
        assert bound["permissions"] == {"contents": "read"}
        assert "secrets." not in yaml.safe_dump(bound)
        checkout = self._step("bound", "actions/checkout@")
        assert checkout["with"]["persist-credentials"] is False

    def test_the_bound_job_does_not_commit(self):
        bound_step = next(
            s
            for s in self.jobs["bound"]["steps"]
            if s.get("name") == "Bound the refreshed locks"
        )
        assert "--no-commit" in bound_step["run"]

    def test_the_artifact_carries_exactly_the_drivers_outputs(self):
        upload = self._step("bound", "actions/upload-artifact@")
        assert upload["with"]["path"].split() == list(orchestrator.OUTPUT_PATHS)

    def test_the_token_job_installs_and_resolves_nothing(self):
        publish = self.jobs["publish"]
        assert publish["needs"] == "bound"
        dumped = yaml.safe_dump(publish)
        for forbidden in ("setup-uv", "setup-node", "uv run", "npm "):
            assert forbidden not in dumped, forbidden
        apply_step = next(
            s for s in publish["steps"] if "bound_lock_branch.py" in s.get("run", "")
        )
        assert "--apply-from" in apply_step["run"]

    def test_both_jobs_work_on_the_pushed_sha(self):
        # `publish` commits onto what `bound` resolved; a moved branch must make
        # the push non-fast-forward, not absorb stale outputs.
        for job in ("bound", "publish"):
            checkout = self._step(job, "actions/checkout@")
            assert checkout["with"]["ref"] == "${{ github.sha }}", job

    def test_the_declared_paths_exist_in_this_repo(self):
        root = Path(__file__).parent.parent.parent.parent
        for path in (*orchestrator.OUTPUT_PATHS, orchestrator.PYATLAN_LOCK):
            assert (root / path).is_file(), path

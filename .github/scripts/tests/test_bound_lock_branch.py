"""Tests for .github/scripts/bound_lock_branch.py.

Pure-unit: the bound itself is stubbed (it has its own suite in
test_renovate_uv_lock_bounded.py), and uv is never invoked. What needs cover here
is the orchestration around it, because every one of its failure modes is silent:
a project skipped, an exempt set that lost a name, or a partial commit that merges
an unbounded lock.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import tomllib
from pathlib import Path

import bound_lock_branch as orchestrator
import pytest
import yaml

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
    """A repo shaped like application-sdk: one workspace lock and the
    relationship table. Every file the refresh lane may rewrite."""
    (tmp_path / "uv.lock").write_text(uv_lock(marker="root"))
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
    """What renovate-lock-cooldown.yaml passes to renovate-branch-fixup.yaml.

    The actor allowlist, the re-entrancy guard and the job split are the
    reusable's, guarded once in test_renovate_branch_fixup.py.
    """

    @property
    def job(self) -> dict:
        workflow = yaml.safe_load(WORKFLOW.read_text())
        return workflow["jobs"]["bound"]

    def test_re_entrancy_is_guarded_by_the_commit_subject_we_actually_write(self):
        # The actor list admits atlan-app-fleet[bot], which is the identity our
        # own push arrives as, so the actor check cannot be what stops the loop.
        # This is, and it only works while it matches the driver's message.
        assert orchestrator.COMMIT_MESSAGE.startswith(
            "chore(deps): bound the refreshed locks"
        )
        assert (
            self.job["with"]["own-commit-subject"]
            == "chore(deps): bound the refreshed locks"
        )

    def test_the_bound_runs_this_driver_with_the_fleet_window(self):
        assert self.job["with"]["script"] == ".github/scripts/bound_lock_branch.py"
        assert self.job["with"]["generate-args"] == "--window P3D"

    def _route(self, ref: str) -> list[str]:
        """The jobs whose `if:` admits a push to ``ref``, if its branch triggers."""
        workflow = yaml.safe_load(WORKFLOW.read_text())
        # YAML 1.1 resolves a bare `on` to the boolean True, so that — not the
        # string — is the key the loader produces for a workflow's trigger block.
        if not _triggers(ref, workflow[True]["push"]["branches"]):
            return []
        jobs: list[str] = []
        for job_id, job in workflow["jobs"].items():
            condition = job["if"]
            target = f"refs/heads/{ref}"
            if condition == f"github.ref == '{target}'" or (
                condition.startswith("github.ref != ")
                and condition != f"github.ref != '{target}'"
            ):
                jobs.append(job_id)
        return jobs

    def test_the_lock_refresh_runs_the_upgrading_bound_alone(self):
        assert self._route("renovate/lock-file-maintenance") == ["bound"]

    @pytest.mark.parametrize(
        "branch",
        [
            "renovate/fastmcp-4.x",
            "renovate/non-critical-python-dependencies",
            "renovate/ruff",
        ],
    )
    def test_range_bump_branches_run_the_no_upgrade_bound_alone(self, branch):
        # FND-5472: #3656 (renovate/fastmcp-4.x) failed dep-cooldown for 83
        # days because nothing bounded a range-bump relock.
        assert self._route(branch) == ["bound-bump"]

    @pytest.mark.parametrize(
        "branch",
        [
            # First-party, no window, and the table-sync lane's branches.
            "renovate/pyatlan",
            "renovate/major-pyatlan",
            # Security updates bypass the cooldown.
            "renovate/pypi-urllib3-vulnerability",
        ],
    )
    def test_first_party_and_security_branches_are_never_bounded(self, branch):
        assert self._route(branch) == []

    def test_the_bump_lane_runs_this_driver_without_upgrade_and_the_same_window(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        bump, refresh = jobs["bound-bump"], jobs["bound"]
        assert bump["uses"] == refresh["uses"]
        assert bump["with"]["script"] == refresh["with"]["script"]
        assert bump["with"]["generate-args"] == "--window P3D --no-upgrade"
        # Same subject: the driver writes one message, so the re-entrancy
        # guard must match it on both lanes.
        assert (
            bump["with"]["own-commit-subject"] == refresh["with"]["own-commit-subject"]
        )
        assert bump["with"]["artifact-name"] != refresh["with"]["artifact-name"]
        assert bump["permissions"] == refresh["permissions"]


def _triggers(branch: str, branches: list[str]) -> bool:
    """GitHub's branch filter: the last matching pattern wins, `!` negates."""
    hit = False
    for pattern in branches:
        negated = pattern.startswith("!")
        glob = pattern.removeprefix("!").replace("**", "*")
        if fnmatch.fnmatchcase(branch, glob):
            hit = not negated
    return hit


class TestProjects:
    """The declared project set, guarded because the entry is load-bearing."""

    def test_the_workspace_lock_is_the_only_project(self):
        """packages/conformance is a member of the root uv workspace (FND-4671),
        so the root lock covers it. A second entry would point the bound at a
        lock that no longer exists and fail every refresh."""
        assert [p.directory for p in orchestrator.PROJECTS] == ["."]
        assert orchestrator.PYATLAN_LOCK == "uv.lock"

    def test_the_root_holds_nothing_and_exempts_pyatlan(self):
        """The workspace consumes neither of its own packages from PyPI:
        atlan-application-sdk IS the root, and the conformance package is a
        workspace member."""
        (root,) = orchestrator.PROJECTS
        assert set(root.exempt) == {"pyatlan"}
        assert set(root.hold) == set()


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

        assert len(calls) == len(orchestrator.PROJECTS) == 1
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

    def test_a_held_package_is_passed_to_the_driver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # No project in PROJECTS holds anything today; keep the passthrough
        # covered for when one does.
        calls: list[list[str]] = []

        def record(argv: list[str]) -> int:
            calls.append(argv)
            return 0

        monkeypatch.setattr(orchestrator.bounded, "main", record)
        project = orchestrator.Project(directory="x", hold=("a", "b"))
        orchestrator.bound_project(project, "P7D", "origin/main", tmp_path)
        argv = calls[0]
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--hold"] == [
            "a",
            "b",
        ]

    def test_no_upgrade_is_passed_to_the_driver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        calls: list[list[str]] = []

        def record(argv: list[str]) -> int:
            calls.append(argv)
            return 0

        monkeypatch.setattr(orchestrator.bounded, "main", record)
        (project,) = orchestrator.PROJECTS
        orchestrator.bound_project(project, "P3D", "origin/main", tmp_path)
        orchestrator.bound_project(
            project, "P3D", "origin/main", tmp_path, no_upgrade=True
        )
        assert "--no-upgrade" not in calls[0]
        assert "--no-upgrade" in calls[1]

    def test_main_forwards_no_upgrade(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        seen: list[bool] = []
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            orchestrator,
            "bound_project",
            lambda project, window, ref, root, *, no_upgrade=False: (
                seen.append(no_upgrade) or 1
            ),
        )
        orchestrator.main(
            ["--window", "P3D", "--baseline-ref", "origin/main", "--no-upgrade"]
        )
        assert seen == [True]


class TestMain:
    """End to end with the bound stubbed. The invariant under test throughout is
    that nothing reaches a commit unless every project was bounded successfully."""

    def _stub_bound(self, monkeypatch, *, rewrite: bool = True):
        """Stand in for the driver. `rewrite=False` models an already-bound branch.

        The rewrite leaves pyatlan where it was, so the regeneration is stubbed
        to fail loudly: none of these tests should reach it.
        """

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            if rewrite:
                (directory / "uv.lock").write_text(uv_lock(marker="bounded"))
            return 0

        monkeypatch.setattr(orchestrator.bounded, "main", fake_main)
        monkeypatch.setattr(
            orchestrator,
            "regenerate_relationship_directions",
            lambda root: pytest.fail("pyatlan did not move; nothing to regenerate"),
        )

    def test_the_bounded_lock_is_committed(self, monkeypatch, in_repo):
        self._stub_bound(monkeypatch)

        assert orchestrator.main(["--window", "P7D", "--baseline-ref", "HEAD"]) == 0
        assert head_files(in_repo) == {"uv.lock"}
        assert head_subject(in_repo) == orchestrator.COMMIT_MESSAGE

    def test_a_failed_bound_commits_nothing_at_all(self, monkeypatch, in_repo):
        """Fail-closed: a bound that fails after writing must not leave its
        partial lock committed — the branch still auto-merges, and it would
        look like the control worked.
        """

        def fake_main(argv: list[str]) -> int:
            directory = Path(argv[argv.index("--project-dir") + 1])
            (directory / "uv.lock").write_text(uv_lock(marker="bounded"))
            return 1

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
        (artifact / missing).unlink()

        with pytest.raises(FileNotFoundError, match=missing):
            orchestrator.main(["--apply-from", str(artifact)])
        assert head_subject(in_repo) == "base"
        assert git(in_repo, "status", "--porcelain").stdout == ""

    def test_apply_from_refuses_a_symlinked_parent_directory(
        self, in_repo, tmp_path_factory
    ):
        """A link on any component, not just the last, escapes the artifact.

        Here `packages/` points at a directory outside it holding a complete
        tree, so every leaf is a regular file and a leaf-only check passes.
        """
        artifact = self._unchanged_artifact(in_repo, tmp_path_factory)
        outside = tmp_path_factory.mktemp("elsewhere")
        shutil.move(str(artifact / "packages"), str(outside / "packages"))
        (outside / orchestrator.PYATLAN_LOCK).write_text(uv_lock(marker="outside"))
        (artifact / "packages").symlink_to(outside / "packages")

        with pytest.raises(ValueError, match="symlink"):
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
    """The split itself is renovate-branch-fixup.yaml's (FND-4928), guarded in
    test_renovate_branch_fixup.py; what stays here is this lane's own data."""

    def test_the_declared_paths_exist_in_this_repo(self):
        root = Path(__file__).parent.parent.parent.parent
        for path in (*orchestrator.OUTPUT_PATHS, orchestrator.PYATLAN_LOCK):
            assert (root / path).is_file(), path

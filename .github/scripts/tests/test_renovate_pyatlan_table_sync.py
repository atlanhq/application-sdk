"""Tests for .github/scripts/renovate_pyatlan_table_sync.py (FND-4439, FND-5018, FND-4671).

The table generator is stubbed; git runs for real against a throwaway repo, as
in test_bound_lock_branch.py. Every branch of the sync has a case, plus drift
guards tying the workflow and renovate.json to the script.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import bound_lock_branch as lanes
import pytest
import renovate_approval_conditions as approval
import renovate_pyatlan_table_sync as sync
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "renovate-pyatlan-table-sync.yaml"
COOLDOWN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "renovate-lock-cooldown.yaml"
AUTO_APPROVE = REPO_ROOT / ".github" / "workflows" / "renovate-auto-approve.yml"
#: The generate/publish jobs both lanes call (FND-4928).
FIXUP = REPO_ROOT / ".github" / "workflows" / "renovate-branch-fixup.yaml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": os.environ.get("PATH", ""),
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, env=GIT_ENV, capture_output=True, text=True
    )


def uv_lock(pyatlan: str, boto3: str = "1.0.0") -> str:
    return (
        f'version = 1\n\n[[package]]\nname = "boto3"\nversion = "{boto3}"\n'
        f'\n[[package]]\nname = "pyatlan"\nversion = "{pyatlan}"\n'
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main: the root lock on pyatlan 11.5.0 and its table. Each test then
    writes its branch's lock on top."""
    (tmp_path / "uv.lock").write_text(uv_lock("11.5.0"))
    table = tmp_path / lanes.RELATIONSHIP_DIRECTIONS
    table.parent.mkdir(parents=True)
    table.write_text('{"table": "11.5.0"}\n')
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "renovate/some-bump")
    monkeypatch.chdir(tmp_path)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@pytest.fixture
def regens(repo: Path, monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Stands in for the generator: records the pyatlan it built the table for."""
    seen: list[str | None] = []

    def regen(root: Path) -> int:
        version = lanes.pyatlan_version((root / lanes.PYATLAN_LOCK).read_text())
        seen.append(version)
        (root / lanes.RELATIONSHIP_DIRECTIONS).write_text(
            json.dumps({"table": version}) + "\n"
        )
        return 0

    monkeypatch.setattr(lanes, "regenerate_relationship_directions", regen)
    return seen


def run_sync() -> int:
    return sync.main(["--no-commit", "--baseline-ref", "main"])


class TestSync:
    def test_the_table_is_read_from_the_workspace_lock(self):
        # packages/conformance has no lock of its own any more (FND-4671).
        assert lanes.PYATLAN_LOCK == "uv.lock"
        assert sync.OUTPUT_PATHS == (lanes.RELATIONSHIP_DIRECTIONS,)

    def test_a_pyatlan_move_regenerates_the_table(
        self, repo: Path, regens: list[str | None]
    ):
        (repo / "uv.lock").write_text(uv_lock("11.6.0"))
        assert run_sync() == 0
        assert regens == ["11.6.0"]
        # --no-commit leaves the outputs for the publishing job.
        assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "base"

    def test_a_bump_that_leaves_pyatlan_alone_is_a_no_op(
        self, repo: Path, regens: list[str | None]
    ):
        # e.g. a loguru bump on renovate/loguru-0.x.
        (repo / "uv.lock").write_text(uv_lock("11.5.0", boto3="1.1.0"))
        assert run_sync() == 0
        assert regens == []
        assert not git(
            repo, "status", "--porcelain", lanes.RELATIONSHIP_DIRECTIONS
        ).stdout

    def test_a_regeneration_failure_fails(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (repo / "uv.lock").write_text(uv_lock("11.6.0"))
        monkeypatch.setattr(lanes, "regenerate_relationship_directions", lambda r: 3)
        assert run_sync() == 3

    def test_no_commit_requires_a_baseline(self, repo: Path):
        with pytest.raises(SystemExit):
            sync.main(["--no-commit"])


class TestApply:
    def _artifact(self, tmp_path: Path, table: str) -> Path:
        art = tmp_path / "artifact"
        (art / lanes.RELATIONSHIP_DIRECTIONS).parent.mkdir(parents=True)
        (art / lanes.RELATIONSHIP_DIRECTIONS).write_text(table)
        return art

    def test_apply_commits_only_the_table_with_our_subject(
        self, repo: Path, tmp_path: Path
    ):
        (repo / "stray.txt").write_text("not ours")
        (repo / "uv.lock").write_text(uv_lock("11.6.0"))  # Renovate's, uncommitted
        art = self._artifact(tmp_path, '{"table": "11.6.0"}\n')
        assert sync.main(["--apply-from", str(art)]) == 0
        assert (
            git(repo, "log", "-1", "--format=%s").stdout.strip() == sync.COMMIT_MESSAGE
        )
        files = set(
            git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        )
        assert files == {lanes.RELATIONSHIP_DIRECTIONS}

    def test_apply_is_idempotent(self, repo: Path, tmp_path: Path):
        art = self._artifact(tmp_path, '{"table": "11.5.0"}\n')  # as on the branch
        head = git(repo, "rev-parse", "HEAD").stdout
        assert sync.main(["--apply-from", str(art)]) == 0
        assert git(repo, "rev-parse", "HEAD").stdout == head

    def test_apply_refuses_a_missing_output(self, repo: Path, tmp_path: Path):
        art = self._artifact(tmp_path, "{}\n")
        (art / lanes.RELATIONSHIP_DIRECTIONS).unlink()
        with pytest.raises(FileNotFoundError):
            sync.main(["--apply-from", str(art)])


@dataclass(frozen=True)
class Step:
    """The parts of a workflow step the handoff depends on."""

    name: str
    run: str
    uses: str
    path: str  # `with.path`, empty when the step has none
    conditional: bool  # has an `if:`, i.e. the upload retry


def _sync_steps() -> list[Step]:
    """The reusable's `generate` steps, with this lane's script substituted for
    the `$SCRIPT` the reusable reads from its `script` input."""
    workflow = cast(
        dict[str, dict[str, dict[str, list[dict[str, object]]]]],
        yaml.safe_load(FIXUP.read_text()),
    )
    steps: list[Step] = []
    for raw in workflow["jobs"]["generate"]["steps"]:
        with_ = cast(dict[str, object], raw.get("with", {}))
        steps.append(
            Step(
                name=str(raw.get("name", "")),
                run=str(raw.get("run", "")).replace('"$SCRIPT"', SCRIPT),
                uses=str(raw.get("uses", "")),
                path=str(with_.get("path", "")),
                conditional="if" in raw,
            )
        )
    return steps


def _upload_root(search_paths: list[Path]) -> Path:
    """The artifact root actions/upload-artifact picks for ``search_paths``.

    Mirrors its search.ts: several paths -> their least common ancestor (the
    rule that bit FND-5015: the root became packages/conformance and the
    prefix was dropped); one directory -> that directory; one file -> its
    parent.
    """
    if len(search_paths) > 1:
        return Path(os.path.commonpath([str(p) for p in search_paths]))
    only = search_paths[0]
    return only if only.is_dir() else only.parent


SCRIPT = ".github/scripts/renovate_pyatlan_table_sync.py"
STAGING_DIR = "fixup-out"


def _run_sync_job_handoff(repo: Path, runner_temp: Path) -> Path:
    """Replay the sync job's steps after the sync itself, then the publish
    job's download, the way the runner would: run any staging step's script
    for real, upload each artifact with upload-artifact's root rule, and
    merge them into one download directory. Returns that directory.

    The retry upload (`if:` the first one failed) carries the same path, which
    the wiring test pins, so replaying the first upload is enough.
    """
    download = runner_temp / "fixup-in"
    for step in _sync_steps():
        if SCRIPT in step.run and "--stage-to" in step.run:
            argv = shlex.split(step.run.replace("$RUNNER_TEMP", str(runner_temp)))
            assert sync.main(argv[argv.index(SCRIPT) + 1 :]) == 0
        if "upload-artifact" in step.uses and not step.conditional:
            raw = step.path.replace("${{ runner.temp }}", str(runner_temp))
            search = [repo / entry for entry in raw.split()]
            root = _upload_root(search)
            for entry in search:
                files = (
                    [entry]
                    if entry.is_file()
                    else [f for f in entry.rglob("*") if f.is_file()]
                )
                for f in files:
                    target = download / f.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, target)
    return download


class TestHandoff:
    """The sync job's artifact must reach publish at repo paths (FND-5015)."""

    def test_the_workflow_handoff_round_trips_every_output(
        self, repo: Path, tmp_path: Path
    ):
        for path in sync.OUTPUT_PATHS:
            (repo / path).write_text(f"synced {path}\n")
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        download = _run_sync_job_handoff(repo, runner_temp)
        assert sync.main(["--apply-from", str(download)]) == 0
        committed = set(
            git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        )
        assert committed == set(sync.OUTPUT_PATHS)

    def test_staging_then_apply_round_trips_every_output(
        self, repo: Path, tmp_path: Path
    ):
        for path in sync.OUTPUT_PATHS:
            (repo / path).write_text(f"synced {path}\n")
        staged = tmp_path / "staged"
        assert sync.main(["--stage-to", str(staged)]) == 0
        staged_files = {
            str(f.relative_to(staged)) for f in staged.rglob("*") if f.is_file()
        }
        assert staged_files == set(sync.OUTPUT_PATHS)
        git(repo, "checkout", "-q", "--", ".")  # back to the branch's outputs
        assert sync.main(["--apply-from", str(staged)]) == 0
        for path in sync.OUTPUT_PATHS:
            assert (repo / path).read_text() == f"synced {path}\n"

    def test_the_old_flattened_layout_is_still_refused(
        self, repo: Path, tmp_path: Path
    ):
        # apply_outputs stays strict: an artifact rooted at the outputs'
        # common parent (the pre-FND-5015 layout) is a broken handoff.
        root = _upload_root([repo / p for p in sync.OUTPUT_PATHS])
        flat = tmp_path / "flat"
        for path in sync.OUTPUT_PATHS:
            target = flat / (repo / path).relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / path, target)
        with pytest.raises(FileNotFoundError, match=sync.OUTPUT_PATHS[0]):
            sync.main(["--apply-from", str(flat)])

    def test_staging_refuses_a_missing_output(self, repo: Path, tmp_path: Path):
        (repo / lanes.RELATIONSHIP_DIRECTIONS).unlink()
        staged = tmp_path / "staged"
        with pytest.raises(FileNotFoundError):
            sync.main(["--stage-to", str(staged)])
        assert not staged.exists()  # checked before anything is copied

    def test_staging_refuses_a_symlinked_output(self, repo: Path, tmp_path: Path):
        outside = tmp_path / "outside.json"
        outside.write_text("{}\n")
        (repo / lanes.RELATIONSHIP_DIRECTIONS).unlink()
        (repo / lanes.RELATIONSHIP_DIRECTIONS).symlink_to(outside)
        with pytest.raises(ValueError, match="symlinked"):
            sync.main(["--stage-to", str(tmp_path / "staged")])

    def test_staging_refuses_a_non_empty_folder(self, repo: Path, tmp_path: Path):
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "stale.txt").write_text("left over\n")
        with pytest.raises(FileExistsError):
            sync.main(["--stage-to", str(staged)])


def _push_filter(path: Path) -> dict[str, list[str]]:
    # YAML 1.1 reads a bare `on` as True.
    return yaml.safe_load(path.read_text())[True]["push"]


def _triggers(branch: str, branches: list[str]) -> bool:
    """GitHub's branch filter: the last matching pattern wins, `!` negates."""
    hit = False
    for pattern in branches:
        negated = pattern.startswith("!")
        glob = pattern.removeprefix("!").replace("**", "*")
        if fnmatch.fnmatchcase(branch, glob):
            hit = not negated
    return hit


class TestWiring:
    """Drift guards: each of these fails silently in production if it drifts."""

    @property
    def workflow(self) -> dict[str, dict[str, dict[str, str]]]:
        return cast(
            dict[str, dict[str, dict[str, str]]], yaml.safe_load(WORKFLOW.read_text())
        )

    def test_it_runs_this_script_through_the_shared_fixup_jobs(self):
        # The actor list, the folded `if:` and the job split are the
        # reusable's, guarded in test_renovate_branch_fixup.py.
        job = cast(dict[str, Any], self.workflow["jobs"]["sync"])
        assert job["uses"] == "./.github/workflows/renovate-branch-fixup.yaml"
        assert job["with"]["script"] == SCRIPT

    def test_re_entrancy_guard_matches_our_commit_subject(self):
        job = cast(dict[str, Any], self.workflow["jobs"]["sync"])
        subject = job["with"]["own-commit-subject"]
        assert sync.COMMIT_MESSAGE.startswith(subject)

    def test_our_own_outputs_never_match_the_paths_filter(self):
        # The other half of re-entrancy: our push changes only OUTPUT_PATHS.
        paths = _push_filter(WORKFLOW)["paths"]
        for output in sync.OUTPUT_PATHS:
            assert not any(fnmatch.fnmatchcase(output, p) for p in paths), output

    def test_every_upload_carries_the_one_folder_the_staging_step_fills(self):
        steps = _sync_steps()
        staging = [
            i for i, step in enumerate(steps) if f"{SCRIPT} --stage-to" in step.run
        ]
        assert len(staging) == 1
        assert shlex.split(steps[staging[0]].run)[-1] == f"$RUNNER_TEMP/{STAGING_DIR}"
        uploads = [
            (i, step) for i, step in enumerate(steps) if "upload-artifact" in step.uses
        ]
        assert len(uploads) == 1  # upload-artifact-retry holds the retry
        for i, step in uploads:
            assert i > staging[0], step.name
            # One folder, never a list of files: several paths make
            # upload-artifact drop their common prefix (FND-5015).
            assert step.path == f"${{{{ runner.temp }}}}/{STAGING_DIR}", step.name

    def test_the_branch_scope_is_every_renovate_branch_but_lock_maintenance(self):
        assert _push_filter(WORKFLOW)["branches"] == [
            "renovate/**",
            "!renovate/lock-file-maintenance",
        ]

    def test_the_excluded_branch_is_the_one_the_cooldown_lane_owns(self):
        # Both lanes regenerate the table; on one branch they would race.
        owned = _push_filter(COOLDOWN_WORKFLOW)["branches"]
        branches = _push_filter(WORKFLOW)["branches"]
        assert owned and all(not _triggers(b, branches) for b in owned)

    @pytest.mark.parametrize(
        "branch",
        ["renovate/pyatlan", "renovate/major-pyatlan", "renovate/loguru-0.x"],
    )
    def test_dependency_branches_trigger_it(self, branch: str):
        assert _triggers(branch, _push_filter(WORKFLOW)["branches"])

    def test_the_pyatlan_branch_renovate_json_groups_onto_is_in_scope(self):
        config = json.loads((REPO_ROOT / "renovate.json").read_text())
        groups = {
            r.get("groupName")
            for r in config["packageRules"]
            if r.get("matchPackageNames") == ["pyatlan"] and "groupName" in r
        }
        # Renovate's branch is renovate/<slugified groupName>.
        assert groups == {"pyatlan"}
        assert _triggers("renovate/pyatlan", _push_filter(WORKFLOW)["branches"])

    def test_pyatlan_majors_are_not_auto_merged(self):
        config = json.loads((REPO_ROOT / "renovate.json").read_text())
        rules = [
            r
            for r in config["packageRules"]
            if r.get("matchPackageNames") == ["pyatlan"]
        ]
        assert rules[-1]["matchUpdateTypes"] == ["major"]
        assert rules[-1]["automerge"] is False
        assert rules[-1]["platformAutomerge"] is False

    def test_the_approval_gate_accepts_the_regenerated_table(self):
        pattern = yaml.safe_load(AUTO_APPROVE.read_text())["jobs"]["auto-approve"][
            "with"
        ]["extra_dep_file_pattern"]
        assert (
            approval.non_dep_files(
                [
                    "pyproject.toml",
                    "uv.lock",
                    "packages/api/pyproject.toml",
                    *sync.OUTPUT_PATHS,
                ],
                pattern,
            )
            == []
        )
        # And nothing else under the data dir rides along.
        assert approval.non_dep_files(
            ["packages/conformance/conformance/data/other.json"], pattern
        ) == ["packages/conformance/conformance/data/other.json"]

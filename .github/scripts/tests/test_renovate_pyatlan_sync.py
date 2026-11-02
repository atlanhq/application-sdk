"""Tests for .github/scripts/renovate_pyatlan_sync.py (FND-4439).

uv and the table generator are stubbed; git runs for real against a throwaway
repo, as in test_bound_lock_branch.py. Every branch of the sync (behind, level,
ahead, root without pyatlan, uv failure, regeneration failure) has a case, plus
drift guards tying the workflow and renovate.json to the script.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import bound_lock_branch as lanes
import renovate_approval_conditions as approval
import renovate_pyatlan_sync as sync

REPO_ROOT = Path(__file__).parent.parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "renovate-pyatlan-sync.yaml"
AUTO_APPROVE = REPO_ROOT / ".github" / "workflows" / "renovate-auto-approve.yml"

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


def uv_lock(pyatlan: str | None) -> str:
    text = 'version = 1\n\n[[package]]\nname = "boto3"\nversion = "1.0.0"\n'
    if pyatlan is not None:
        text += f'\n[[package]]\nname = "pyatlan"\nversion = "{pyatlan}"\n'
    return text


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """main has pyatlan 11.5.0 in both locks; the branch's root lock has 11.6.0."""
    (tmp_path / "uv.lock").write_text(uv_lock("11.5.0"))
    conf = tmp_path / lanes.PYATLAN_LOCK
    conf.parent.mkdir(parents=True)
    conf.write_text(uv_lock("11.5.0"))
    table = tmp_path / lanes.RELATIONSHIP_DIRECTIONS
    table.parent.mkdir(parents=True)
    table.write_text('{"table": "11.5.0"}\n')
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "renovate/pyatlan")
    (tmp_path / "uv.lock").write_text(uv_lock("11.6.0"))
    git(tmp_path, "commit", "-qam", "chore(deps): update pyatlan")
    monkeypatch.chdir(tmp_path)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@pytest.fixture
def calls(monkeypatch) -> dict[str, list]:
    """Stub uv: the upgrade rewrites the conformance lock to the asked version,
    and the regeneration rewrites the table. Records what was called."""
    record: dict[str, list] = {"upgrade": [], "regen": []}

    def fake_upgrade(version: str, root: Path) -> int:
        record["upgrade"].append(version)
        (root / lanes.PYATLAN_LOCK).write_text(uv_lock(version))
        return 0

    def fake_regen(root: Path) -> int:
        version = lanes.pyatlan_version((root / lanes.PYATLAN_LOCK).read_text())
        record["regen"].append(version)
        (root / lanes.RELATIONSHIP_DIRECTIONS).write_text(
            json.dumps({"table": version}) + "\n"
        )
        return 0

    monkeypatch.setattr(sync, "upgrade_conformance_pyatlan", fake_upgrade)
    monkeypatch.setattr(lanes, "regenerate_relationship_directions", fake_regen)
    return record


class TestNeedsSync:
    def test_behind_needs_a_sync(self):
        assert sync.needs_sync("11.6.0", "11.5.0")

    def test_level_does_not(self):
        assert not sync.needs_sync("11.6.0", "11.6.0")

    def test_ahead_never_moves_backwards(self):
        # The lock refresh exempts pyatlan from its cooldown and may get there
        # first; a floor PR built before it must not roll the lock back.
        assert not sync.needs_sync("11.6.0", "11.7.0")

    def test_versions_compare_as_pep440_not_strings(self):
        assert sync.needs_sync("11.10.0", "11.9.0")

    def test_missing_conformance_pyatlan_needs_a_sync(self):
        assert sync.needs_sync("11.6.0", None)

    def test_a_root_lock_without_pyatlan_is_an_error(self):
        with pytest.raises(ValueError):
            sync.needs_sync(None, "11.5.0")


class TestSync:
    def test_behind_moves_the_lock_to_the_root_version_and_regenerates(
        self, repo, calls
    ):
        assert sync.main(["--no-commit", "--baseline-ref", "main"]) == 0
        assert calls["upgrade"] == ["11.6.0"]
        assert calls["regen"] == ["11.6.0"]
        # --no-commit leaves the outputs for the publishing job.
        assert git(repo, "log", "-1", "--format=%s").stdout.strip() == (
            "chore(deps): update pyatlan"
        )

    def test_level_with_main_changes_nothing(self, repo, calls):
        (repo / "uv.lock").write_text(uv_lock("11.5.0"))
        assert sync.main(["--no-commit", "--baseline-ref", "main"]) == 0
        assert calls == {"upgrade": [], "regen": []}

    def test_ahead_of_root_is_left_alone_but_the_table_still_follows_the_lock(
        self, repo, calls
    ):
        # The conformance lock already moved past main (e.g. a rebase onto a
        # refresh that has not merged yet): no upgrade, but the table must match
        # the lock that will merge.
        (repo / lanes.PYATLAN_LOCK).write_text(uv_lock("11.7.0"))
        assert sync.main(["--no-commit", "--baseline-ref", "main"]) == 0
        assert calls["upgrade"] == []
        assert calls["regen"] == ["11.7.0"]

    def test_a_uv_failure_fails_and_skips_regeneration(self, repo, calls, monkeypatch):
        monkeypatch.setattr(sync, "upgrade_conformance_pyatlan", lambda v, r: 2)
        assert sync.main(["--no-commit", "--baseline-ref", "main"]) == 2
        assert calls["regen"] == []

    def test_a_regeneration_failure_fails(self, repo, calls, monkeypatch):
        monkeypatch.setattr(lanes, "regenerate_relationship_directions", lambda r: 3)
        assert sync.main(["--no-commit", "--baseline-ref", "main"]) == 3

    def test_no_commit_requires_a_baseline(self, repo):
        with pytest.raises(SystemExit):
            sync.main(["--no-commit"])

    def test_upgrade_pins_the_exact_version(self, monkeypatch, tmp_path):
        seen = []

        def fake_run(argv, cwd):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(sync.subprocess, "run", fake_run)
        assert sync.upgrade_conformance_pyatlan("11.6.0", tmp_path) == 0
        assert seen == [
            [
                "uv",
                "lock",
                "--directory",
                "packages/conformance",
                "--upgrade-package",
                "pyatlan==11.6.0",
            ]
        ]


class TestApply:
    def _artifact(self, tmp_path: Path, version: str) -> Path:
        art = tmp_path / "artifact"
        for path in sync.OUTPUT_PATHS:
            (art / path).parent.mkdir(parents=True, exist_ok=True)
        (art / lanes.PYATLAN_LOCK).write_text(uv_lock(version))
        (art / lanes.RELATIONSHIP_DIRECTIONS).write_text(
            json.dumps({"table": version}) + "\n"
        )
        return art

    def test_apply_commits_only_the_outputs_with_our_subject(self, repo, tmp_path):
        (repo / "stray.txt").write_text("not ours")
        art = self._artifact(tmp_path, "11.6.0")
        assert sync.main(["--apply-from", str(art)]) == 0
        assert (
            git(repo, "log", "-1", "--format=%s").stdout.strip() == sync.COMMIT_MESSAGE
        )
        files = set(
            git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        )
        assert files == set(sync.OUTPUT_PATHS)

    def test_apply_is_idempotent(self, repo, tmp_path):
        art = self._artifact(tmp_path, "11.5.0")  # identical to the branch
        head = git(repo, "rev-parse", "HEAD").stdout
        assert sync.main(["--apply-from", str(art)]) == 0
        assert git(repo, "rev-parse", "HEAD").stdout == head

    def test_apply_refuses_a_missing_output(self, repo, tmp_path):
        art = self._artifact(tmp_path, "11.6.0")
        (art / lanes.RELATIONSHIP_DIRECTIONS).unlink()
        with pytest.raises(FileNotFoundError):
            sync.main(["--apply-from", str(art)])


class TestWiring:
    """Drift guards: each of these fails silently in production if it drifts."""

    @property
    def workflow(self) -> dict:
        return yaml.safe_load(WORKFLOW.read_text())

    def test_the_actor_list_matches_the_approval_gates_renovate_identities(self):
        declared = re.findall(
            r'"([a-z0-9-]+\[bot\])"', self.workflow["jobs"]["sync"]["if"]
        )
        assert sorted(declared) == sorted(approval.RENOVATE_AUTHORS)

    def test_the_condition_folds_to_a_single_line(self):
        assert "\n" not in self.workflow["jobs"]["sync"]["if"]

    def test_re_entrancy_guard_matches_our_commit_subject(self):
        guard = re.search(
            r"startsWith\(github\.event\.head_commit\.message, '([^']+)'\)",
            self.workflow["jobs"]["sync"]["if"],
        )
        assert guard and sync.COMMIT_MESSAGE.startswith(guard.group(1))

    def test_the_artifact_carries_exactly_the_output_paths(self):
        for step in self.workflow["jobs"]["sync"]["steps"]:
            if "upload-artifact" in step.get("uses", ""):
                paths = step["with"]["path"].split()
                assert sorted(paths) == sorted(sync.OUTPUT_PATHS), step["name"]

    def test_only_the_minor_patch_branch_triggers_it(self):
        # YAML 1.1 reads a bare `on` as True.
        assert self.workflow[True]["push"]["branches"] == ["renovate/pyatlan"]

    def test_the_branch_is_the_one_renovate_json_groups_pyatlan_onto(self):
        config = json.loads((REPO_ROOT / "renovate.json").read_text())
        groups = {
            r.get("groupName")
            for r in config["packageRules"]
            if r.get("matchPackageNames") == ["pyatlan"] and "groupName" in r
        }
        # Renovate's branch is renovate/<slugified groupName>.
        assert groups == {"pyatlan"}

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

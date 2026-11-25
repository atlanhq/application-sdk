"""Tests for .github/scripts/renovate_conformance_lock_sync.py (FND-4439, FND-5018).

uv and the table generator are stubbed; git runs for real against a throwaway
repo, as in test_bound_lock_branch.py. The drift comparison is the real one
from conformance_wheel_gate, pointed at the throwaway repo's manifests. Every
branch of the sync has a case, plus drift guards tying the workflow and
renovate.json to the script.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import bound_lock_branch as lanes
import conformance_wheel_gate as gate
import pytest
import renovate_approval_conditions as approval
import renovate_conformance_lock_sync as sync
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "renovate-conformance-lock-sync.yaml"
COOLDOWN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "renovate-lock-cooldown.yaml"
AUTO_APPROVE = REPO_ROOT / ".github" / "workflows" / "renovate-auto-approve.yml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": os.environ.get("PATH", ""),
}

SDK = "atlan-application-sdk"
API = "atlan-application-sdk-api"

#: What main declares and locks: the SDK on loguru 0.7.x and pyatlan 11.5.0.
MAIN_DECLARED = {"loguru": ">=0.7.3,<0.8.0", "pyatlan": ">=11.5.0,<12"}
MAIN_LOCKED = {"boto3": "1.0.0", "loguru": "0.7.3", "pyatlan": "11.5.0"}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, env=GIT_ENV, capture_output=True, text=True
    )


def uv_lock(versions: dict[str, str], sdk_requires: dict[str, str]) -> str:
    """A uv.lock with the two path-sourced packages and ``versions``.

    ``sdk_requires`` is what the lock recorded the SDK as declaring.
    """
    requires = ", ".join(
        f'{{ name = "{name}", specifier = "{spec}" }}'
        for name, spec in sorted(sdk_requires.items())
    )
    text = (
        "version = 1\n\n"
        f'[[package]]\nname = "{SDK}"\nversion = "3.0.0"\n'
        'source = { editable = "../../" }\n\n'
        f"[package.metadata]\nrequires-dist = [{requires}]\n\n"
        f'[[package]]\nname = "{API}"\nversion = "3.0.0"\n'
        'source = { editable = "../api" }\n\n'
        "[package.metadata]\nrequires-dist = []\n"
    )
    for name, version in sorted(versions.items()):
        text += f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
    return text


def pyproject(declared: dict[str, str]) -> str:
    deps = ", ".join(f'"{name}{spec}"' for name, spec in sorted(declared.items()))
    return f'[project]\nname = "x"\ndependencies = [{deps}]\n'


def write_branch(
    repo: Path,
    declared: dict[str, str],
    root_locked: dict[str, str],
    conformance_locked: dict[str, str] | None = None,
) -> None:
    """Put the branch's manifests and locks in place (uncommitted is fine)."""
    (repo / "pyproject.toml").write_text(pyproject(declared))
    (repo / "uv.lock").write_text(uv_lock(root_locked, declared))
    if conformance_locked is not None:
        (repo / lanes.PYATLAN_LOCK).write_text(
            uv_lock(conformance_locked, MAIN_DECLARED)
        )


def conformance_versions(repo: Path) -> dict[str, str]:
    """The conformance lock's third-party versions (not the path sources)."""
    versions = sync.locked_versions((repo / lanes.PYATLAN_LOCK).read_text())
    return {k: v for k, v in versions.items() if k not in (SDK, API)}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main: everything on loguru 0.7.3 / pyatlan 11.5.0, both locks current.

    Each test then writes its branch state with ``write_branch``.
    """
    (tmp_path / "packages" / "api").mkdir(parents=True)
    (tmp_path / "packages" / "api" / "pyproject.toml").write_text(pyproject({}))
    write_branch(tmp_path, MAIN_DECLARED, MAIN_LOCKED)
    conf = tmp_path / lanes.PYATLAN_LOCK
    conf.parent.mkdir(parents=True)
    conf.write_text(uv_lock(MAIN_LOCKED, MAIN_DECLARED))
    table = tmp_path / lanes.RELATIONSHIP_DIRECTIONS
    table.parent.mkdir(parents=True)
    table.write_text('{"table": "11.5.0"}\n')
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "renovate/some-bump")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        gate, "LOCAL_PACKAGES", {SDK: tmp_path, API: tmp_path / "packages" / "api"}
    )
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@dataclass
class FakeUv:
    """Stands in for ``uv lock`` and the table generator.

    The relock does what uv does with ``--upgrade-package``: it records the
    SDK's current declarations, keeps every existing version except the pinned
    ones, and adds ``new_dependencies`` (at their own version, unless pinned).
    ``pulled_in_by`` maps ``name==version`` to the packages that exact version
    depends on, added the same way: pinning to another version of a package
    can bring in dependencies of its own.
    """

    repo: Path
    new_dependencies: dict[str, str]
    relocks: list[dict[str, str]]
    regens: list[str | None]
    records_declarations: bool = True
    pulled_in_by: dict[str, dict[str, str]] = field(
        default_factory=dict[str, dict[str, str]]
    )

    def relock(self, pins: dict[str, str], root: Path) -> int:
        self.relocks.append(dict(pins))
        lock = root / lanes.PYATLAN_LOCK
        versions = {
            **sync.locked_versions(lock.read_text()),
            **self.new_dependencies,
        }
        versions.update(pins)
        for name, version in list(versions.items()):
            for dep, dep_version in self.pulled_in_by.get(
                f"{name}=={version}", {}
            ).items():
                versions.setdefault(dep, dep_version)
        versions.update(pins)
        project = gate.read_lock(lock)
        declared = (
            {
                req[0]: ",".join(req[2])
                for req in gate.declared_requirements(root / "pyproject.toml", ())
            }
            if self.records_declarations
            else {
                req[0]: ",".join(req[2])
                for req in gate.locked_requirements(project, SDK, ()) or set()
            }
        )
        versions = {k: v for k, v in versions.items() if k not in (SDK, API)}
        lock.write_text(uv_lock(versions, declared))
        return 0

    def regen(self, root: Path) -> int:
        version = lanes.pyatlan_version((root / lanes.PYATLAN_LOCK).read_text())
        self.regens.append(version)
        (root / lanes.RELATIONSHIP_DIRECTIONS).write_text(
            json.dumps({"table": version}) + "\n"
        )
        return 0


@pytest.fixture
def uv(repo: Path, monkeypatch: pytest.MonkeyPatch) -> FakeUv:
    fake = FakeUv(repo=repo, new_dependencies={}, relocks=[], regens=[])
    monkeypatch.setattr(sync, "relock", fake.relock)
    monkeypatch.setattr(lanes, "regenerate_relationship_directions", fake.regen)
    return fake


def run_sync() -> int:
    return sync.main(["--no-commit", "--baseline-ref", "main"])


class TestNeedsSync:
    def test_behind_needs_a_sync(self):
        assert sync.needs_sync("11.6.0", "11.5.0")

    def test_level_does_not(self):
        assert not sync.needs_sync("11.6.0", "11.6.0")

    def test_ahead_never_moves_backwards(self):
        # The lock refresh may get there first; a bump PR built before it must
        # not roll the lock back.
        assert not sync.needs_sync("11.6.0", "11.7.0")

    def test_versions_compare_as_pep440_not_strings(self):
        assert sync.needs_sync("11.10.0", "11.9.0")

    def test_missing_from_the_conformance_lock_needs_a_sync(self):
        assert sync.needs_sync("11.6.0", None)

    def test_nothing_at_the_root_to_pin_to_is_no_pin(self):
        assert not sync.needs_sync(None, "1.0.0")


class TestPlanPins:
    def test_a_drifted_package_behind_the_root_is_pinned_to_the_root(self):
        root = {"loguru": "0.8.0", "pyatlan": "11.5.0", "boto3": "1.1.0"}
        conformance = {"loguru": "0.7.3", "pyatlan": "11.5.0", "boto3": "1.0.0"}
        # boto3 is behind too but did not drift: the relock leaves it alone.
        assert sync.plan_pins({"loguru"}, root, conformance) == {"loguru": "0.8.0"}

    def test_pyatlan_behind_is_pinned_without_any_drift(self):
        assert sync.plan_pins(set(), {"pyatlan": "11.6.0"}, {"pyatlan": "11.5.0"}) == {
            "pyatlan": "11.6.0"
        }

    def test_a_drifted_package_ahead_of_the_root_is_not_pinned(self):
        assert (
            sync.plan_pins(
                {"loguru"},
                {"loguru": "0.8.0", "pyatlan": "1"},
                {"loguru": "0.8.1", "pyatlan": "1"},
            )
            == {}
        )

    def test_a_drifted_package_the_root_lacks_is_left_to_the_relock(self):
        assert (
            sync.plan_pins({"gone"}, {"pyatlan": "1"}, {"gone": "1", "pyatlan": "1"})
            == {}
        )

    def test_the_path_sourced_packages_are_never_pinned(self):
        root = {SDK: "3.1.0", "pyatlan": "1"}
        assert sync.plan_pins({SDK}, root, {SDK: "3.0.0", "pyatlan": "1"}) == {}

    def test_a_root_lock_without_pyatlan_is_an_error(self):
        with pytest.raises(ValueError):
            sync.plan_pins(set(), {"loguru": "0.8.0"}, {"pyatlan": "11.5.0"})


class TestSync:
    def test_a_drifted_dependency_moves_to_the_roots_version_and_nothing_else(
        self, repo: Path, uv: FakeUv
    ):
        # The application-sdk#4912 shape: loguru's range moved, the root lock
        # followed, the conformance lock did not.
        declared = {**MAIN_DECLARED, "loguru": ">=0.8.0,<0.9.0"}
        write_branch(
            repo, declared, {**MAIN_LOCKED, "loguru": "0.8.0", "boto3": "1.1.0"}
        )
        assert gate.lock_drift(repo / lanes.PYATLAN_LOCK)  # red before

        assert run_sync() == 0

        assert uv.relocks == [{"loguru": "0.8.0"}]
        assert conformance_versions(repo) == {**MAIN_LOCKED, "loguru": "0.8.0"}
        assert gate.lock_drift(repo / lanes.PYATLAN_LOCK) == []  # green after
        assert uv.regens == []  # pyatlan did not move
        # --no-commit leaves the outputs for the publishing job.
        assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "base"

    def test_no_drift_is_a_no_op(self, repo: Path, uv: FakeUv):
        # e.g. a github-actions or dev-only bump on a renovate/** branch.
        write_branch(repo, MAIN_DECLARED, {**MAIN_LOCKED, "boto3": "1.1.0"})
        assert run_sync() == 0
        assert uv.relocks == [] and uv.regens == []
        assert not git(repo, "status", "--porcelain", lanes.PYATLAN_LOCK).stdout

    def test_a_pyatlan_bump_moves_the_lock_and_regenerates_the_table(
        self, repo: Path, uv: FakeUv
    ):
        declared = {**MAIN_DECLARED, "pyatlan": ">=11.6.0,<12"}
        write_branch(repo, declared, {**MAIN_LOCKED, "pyatlan": "11.6.0"})
        assert run_sync() == 0
        assert uv.relocks == [{"pyatlan": "11.6.0"}]
        assert uv.regens == ["11.6.0"]

    def test_a_pyatlan_floor_already_locked_at_the_root_still_moves_the_lock(
        self, repo: Path, uv: FakeUv
    ):
        # A root lock already on 11.6.0 under an unchanged declaration: no
        # drift, but the conformance lock is behind and the table follows it.
        write_branch(repo, MAIN_DECLARED, {**MAIN_LOCKED, "pyatlan": "11.6.0"})
        assert run_sync() == 0
        assert uv.relocks == [{"pyatlan": "11.6.0"}]
        assert uv.regens == ["11.6.0"]

    def test_ahead_of_root_is_left_alone_but_the_table_still_follows_the_lock(
        self, repo: Path, uv: FakeUv
    ):
        # The conformance lock already moved past main (e.g. a rebase onto a
        # refresh that has not merged yet): no upgrade, but the table must
        # match the lock that will merge.
        write_branch(
            repo,
            MAIN_DECLARED,
            {**MAIN_LOCKED, "pyatlan": "11.6.0"},
            conformance_locked={**MAIN_LOCKED, "pyatlan": "11.7.0"},
        )
        assert run_sync() == 0
        assert uv.relocks == []
        assert uv.regens == ["11.7.0"]

    def test_a_new_dependencys_own_dependencies_are_pinned_to_the_root(
        self, repo: Path, uv: FakeUv
    ):
        # The SDK gains `newdep`, which pulls in `transitive`. A free relock
        # would take whatever is newest; the root's picks went through the
        # cooldown, so the second relock pins both to them.
        declared = {**MAIN_DECLARED, "newdep": ">=2.0"}
        write_branch(
            repo,
            declared,
            {**MAIN_LOCKED, "newdep": "2.0.0", "transitive": "1.4.0"},
        )
        uv.new_dependencies = {"newdep": "2.1.0", "transitive": "1.5.0"}
        assert run_sync() == 0
        assert uv.relocks == [
            {"newdep": "2.0.0"},
            {"newdep": "2.0.0", "transitive": "1.4.0"},
        ]
        assert conformance_versions(repo)["transitive"] == "1.4.0"

    def test_packages_a_re_pin_pulls_in_are_pinned_too(self, repo: Path, uv: FakeUv):
        # F-b4e3de: pinning `transitive` back to the root's 1.4.0 pulls in
        # `deeper`, which the first relock never saw. A single re-scan of the
        # first relock would leave `deeper` at its free 3.1.0.
        declared = {**MAIN_DECLARED, "newdep": ">=2.0"}
        write_branch(
            repo,
            declared,
            {
                **MAIN_LOCKED,
                "newdep": "2.0.0",
                "transitive": "1.4.0",
                "deeper": "3.0.0",
            },
        )
        uv.new_dependencies = {"newdep": "2.1.0", "transitive": "1.5.0"}
        uv.pulled_in_by = {"transitive==1.4.0": {"deeper": "3.1.0"}}
        assert run_sync() == 0
        assert uv.relocks == [
            {"newdep": "2.0.0"},
            {"newdep": "2.0.0", "transitive": "1.4.0"},
            {"newdep": "2.0.0", "transitive": "1.4.0", "deeper": "3.0.0"},
        ]
        assert conformance_versions(repo)["deeper"] == "3.0.0"

    def test_pins_that_never_settle_fail(
        self, repo: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch
    ):
        # Every relock adds a fresh package off the root's version: the loop
        # must stop at the cap and fail rather than commit or spin forever.
        rounds = sync.MAX_PIN_ROUNDS + 2
        root = {**MAIN_LOCKED, **{f"p{i}": "1.0.0" for i in range(rounds)}}
        write_branch(repo, MAIN_DECLARED, {**root, "pyatlan": "11.6.0"})
        calls: list[dict[str, str]] = []

        def runaway(pins: dict[str, str], root_dir: Path) -> int:
            calls.append(dict(pins))
            lock = root_dir / lanes.PYATLAN_LOCK
            versions = {
                k: v
                for k, v in sync.locked_versions(lock.read_text()).items()
                if k not in (SDK, API)
            }
            versions.update(pins)
            versions[f"p{len(calls) - 1}"] = "2.0.0"
            lock.write_text(uv_lock(versions, MAIN_DECLARED))
            return 0

        monkeypatch.setattr(sync, "relock", runaway)
        assert run_sync() == 1
        assert len(calls) == sync.MAX_PIN_ROUNDS + 1
        assert uv.regens == []

    def test_a_dropped_dependency_relocks_with_no_pins(self, repo: Path, uv: FakeUv):
        declared = {"pyatlan": MAIN_DECLARED["pyatlan"]}
        write_branch(repo, declared, MAIN_LOCKED)
        assert run_sync() == 0
        assert uv.relocks == [{}]
        assert gate.lock_drift(repo / lanes.PYATLAN_LOCK) == []

    def test_a_relock_that_leaves_drift_fails(self, repo: Path, uv: FakeUv):
        declared = {**MAIN_DECLARED, "loguru": ">=0.8.0,<0.9.0"}
        write_branch(repo, declared, {**MAIN_LOCKED, "loguru": "0.8.0"})
        uv.records_declarations = False
        assert run_sync() == 1
        assert uv.regens == []

    def test_a_uv_failure_fails_and_skips_regeneration(
        self, repo: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch
    ):
        write_branch(repo, MAIN_DECLARED, {**MAIN_LOCKED, "pyatlan": "11.6.0"})
        monkeypatch.setattr(sync, "relock", lambda pins, root: 2)
        assert run_sync() == 2
        assert uv.regens == []

    def test_a_regeneration_failure_fails(
        self, repo: Path, uv: FakeUv, monkeypatch: pytest.MonkeyPatch
    ):
        write_branch(repo, MAIN_DECLARED, {**MAIN_LOCKED, "pyatlan": "11.6.0"})
        monkeypatch.setattr(lanes, "regenerate_relationship_directions", lambda r: 3)
        assert run_sync() == 3

    def test_no_commit_requires_a_baseline(self, repo: Path):
        with pytest.raises(SystemExit):
            sync.main(["--no-commit"])

    def test_relock_pins_each_exact_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        seen: list[list[str]] = []

        def fake_run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(sync.subprocess, "run", fake_run)
        assert sync.relock({"pyatlan": "11.6.0", "loguru": "0.8.0"}, tmp_path) == 0
        assert seen == [
            [
                "uv",
                "lock",
                "--directory",
                "packages/conformance",
                "--upgrade-package",
                "loguru==0.8.0",
                "--upgrade-package",
                "pyatlan==11.6.0",
            ]
        ]


UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv not on PATH")


def build_wheel(dest: Path, name: str, version: str, requires: list[str]) -> None:
    """A minimal pure-python wheel: enough metadata for ``uv lock``."""
    dist = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {req}\n" for req in requires)
    wheel = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    with zipfile.ZipFile(dest / f"{name}-{version}-py3-none-any.whl", "w") as zf:
        zf.writestr(f"{name}/__init__.py", "")
        zf.writestr(f"{dist}/METADATA", metadata)
        zf.writestr(f"{dist}/WHEEL", wheel)
        zf.writestr(f"{dist}/RECORD", "")


def project(name: str, deps: list[str], sources: dict[str, str] | None = None) -> str:
    text = (
        f'[project]\nname = "{name}"\nversion = "1.0.0"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [{', '.join(json.dumps(d) for d in deps)}]\n\n"
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    )
    if sources:
        text += "\n[tool.uv.sources]\n"
        for dep, path in sources.items():
            text += f'{dep} = {{ path = "{path}", editable = true }}\n'
    return text


@needs_uv
class TestRealUvLock:
    """F-96d5d1: the relock against a lock uv itself wrote, not a hand-made one.

    Offline: every distribution is a local wheel served with --find-links and
    no index, so this exercises uv's real lock format, metadata recording and
    ``--upgrade-package name==version`` resolution without the network.
    """

    @pytest.fixture
    def real(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        build_wheel(wheels, "pyatlan", "11.5.0", [])
        build_wheel(wheels, "loguru", "0.7.3", [])
        build_wheel(wheels, "loguru", "0.8.0", ["colorama>=0.4"])
        build_wheel(wheels, "loguru", "0.8.1", [])
        build_wheel(wheels, "colorama", "0.4.5", [])
        build_wheel(wheels, "colorama", "0.4.6", [])
        repo = tmp_path / "repo"
        conformance = repo / sync.CONFORMANCE_DIR
        (repo / "packages" / "api").mkdir(parents=True)
        conformance.mkdir(parents=True)
        (repo / "pyproject.toml").write_text(
            project(SDK, ["loguru>=0.7.3,<0.8.0", "pyatlan>=11.5.0,<12"])
        )
        (repo / "packages" / "api" / "pyproject.toml").write_text(project(API, []))
        (conformance / "pyproject.toml").write_text(
            project(
                "atlan-application-sdk-conformance",
                [SDK, API],
                {SDK: "../..", API: "../api"},
            )
        )
        for key, value in {
            "UV_NO_INDEX": "1",
            "UV_FIND_LINKS": str(wheels),
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(
            gate, "LOCAL_PACKAGES", {SDK: repo, API: repo / "packages" / "api"}
        )
        assert sync.relock({}, repo) == 0  # main's lock, written by uv
        return repo

    def test_a_bump_relocks_to_the_roots_picks_not_the_newest(self, real: Path):
        # The SDK moves to loguru 0.8.x. The root lock (Renovate's, after the
        # cooldown) took 0.8.0 and colorama 0.4.5; 0.8.1 and colorama 0.4.6
        # are newer and must not be picked.
        lock = real / lanes.PYATLAN_LOCK
        assert sync.locked_versions(lock.read_text())["loguru"] == "0.7.3"
        (real / "pyproject.toml").write_text(
            project(SDK, ["loguru>=0.8.0,<0.9.0", "pyatlan>=11.5.0,<12"])
        )
        assert gate.lock_drift(lock)  # red: uv's own lock records the old range
        (real / "uv.lock").write_text(
            uv_lock(
                {"loguru": "0.8.0", "colorama": "0.4.5", "pyatlan": "11.5.0"},
                {},
            )
        )
        before = sync.locked_versions(lock.read_text())
        root = sync.locked_versions((real / "uv.lock").read_text())
        pins = sync.plan_pins(gate.drifted_packages(lock), root, before)
        assert pins == {"loguru": "0.8.0"}

        assert sync.relock_until_settled(pins, before, root, real) == 0

        after = sync.locked_versions(lock.read_text())
        assert after["loguru"] == "0.8.0"
        assert after["colorama"] == "0.4.5"  # pulled in by 0.8.0, root's pick
        assert gate.lock_drift(lock) == []  # green


class TestApply:
    def _artifact(self, tmp_path: Path, text: str) -> Path:
        art = tmp_path / "artifact"
        for path in sync.OUTPUT_PATHS:
            (art / path).parent.mkdir(parents=True, exist_ok=True)
        (art / lanes.PYATLAN_LOCK).write_text(text)
        (art / lanes.RELATIONSHIP_DIRECTIONS).write_text('{"table": "11.5.0"}\n')
        return art

    def test_apply_commits_only_the_outputs_with_our_subject(
        self, repo: Path, tmp_path: Path
    ):
        (repo / "stray.txt").write_text("not ours")
        art = self._artifact(tmp_path, uv_lock({"pyatlan": "11.6.0"}, MAIN_DECLARED))
        assert sync.main(["--apply-from", str(art)]) == 0
        assert (
            git(repo, "log", "-1", "--format=%s").stdout.strip() == sync.COMMIT_MESSAGE
        )
        files = set(
            git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        )
        assert files == {lanes.PYATLAN_LOCK}

    def test_apply_is_idempotent(self, repo: Path, tmp_path: Path):
        # identical to the branch
        art = self._artifact(tmp_path, uv_lock(MAIN_LOCKED, MAIN_DECLARED))
        head = git(repo, "rev-parse", "HEAD").stdout
        assert sync.main(["--apply-from", str(art)]) == 0
        assert git(repo, "rev-parse", "HEAD").stdout == head

    def test_apply_refuses_a_missing_output(self, repo: Path, tmp_path: Path):
        art = self._artifact(tmp_path, uv_lock(MAIN_LOCKED, MAIN_DECLARED))
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
    workflow = cast(
        dict[str, dict[str, dict[str, list[dict[str, object]]]]],
        yaml.safe_load(WORKFLOW.read_text()),
    )
    steps: list[Step] = []
    for raw in workflow["jobs"]["sync"]["steps"]:
        with_ = cast(dict[str, object], raw.get("with", {}))
        steps.append(
            Step(
                name=str(raw.get("name", "")),
                run=str(raw.get("run", "")),
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


SCRIPT = ".github/scripts/renovate_conformance_lock_sync.py"
STAGING_DIR = "conformance-lock-sync-out"


def _run_sync_job_handoff(repo: Path, runner_temp: Path) -> Path:
    """Replay the sync job's steps after the sync itself, then the publish
    job's download, the way the runner would: run any staging step's script
    for real, upload each artifact with upload-artifact's root rule, and
    merge them into one download directory. Returns that directory.

    The retry upload (`if:` the first one failed) carries the same path, which
    the wiring test pins, so replaying the first upload is enough.
    """
    download = runner_temp / "conformance-lock-sync"
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
        # Both lanes rewrite the conformance lock; on one branch they would race.
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

    def test_the_approval_gate_accepts_the_synced_outputs(self):
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

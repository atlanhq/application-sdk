"""Tests for .github/scripts/renovate_npm_lock_bounded.py.

Pure-unit: npm is never invoked. The resolve is stubbed, because what needs cover
is not "does npm work" but every way this can go wrong quietly —

* a comparison that reads a downgrade as an upgrade (the PEP 440 trap),
* a gate that fires on a tree reshuffle and so never lets the file move,
* a decline that writes back something other than exactly what the base branch
  ships, which is the one thing the mechanism promises it can never do,
* and a resolve that runs with the old lock still in place, where ``--before`` is
  a documented no-op and the whole bound silently evaporates.

The last one is why the invariant is asserted from inside the stub.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_npm_lock_bounded as npm

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

MANIFEST = {
    "name": "remediation",
    "version": "1.0.0",
    "devDependencies": {"@openprose/reactor": "^0.3.1"},
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, env=GIT_ENV, capture_output=True, text=True
    )


def lock(**names: str) -> str:
    """An npm lockfile pinning each name at a version, hoisted under node_modules.

    Keyword form for the common case; `lock_at` takes explicit install paths for
    the nested-copy and reshuffle cases.
    """
    return lock_at({f"node_modules/{name}": version for name, version in names.items()})


def lock_at(paths: dict[str, str]) -> str:
    """An npm lockfile pinning the given install paths at the given versions."""
    packages: dict[str, dict] = {
        "": {"name": "remediation", "version": "1.0.0", "devDependencies": {}}
    }
    for path, version in paths.items():
        packages[path] = {"version": version, "dev": True}
    return json.dumps(
        {"name": "remediation", "lockfileVersion": 3, "packages": packages}, indent=2
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git repo whose HEAD is the baseline and whose worktree is the refresh.

    Shaped like the real one: the npm project sits several directories down, which
    is what makes the `<rev>:./<path>` form in committed_text load-bearing. A
    decoy lock at the repo root catches a driver that reads the wrong file.
    """
    (tmp_path / "package-lock.json").write_text(lock(decoy="9.9.9"))
    directory = tmp_path / "packages" / "conformance" / "conformance"
    directory.mkdir(parents=True)
    (directory / "package.json").write_text(json.dumps(MANIFEST, indent=2))
    (directory / "package-lock.json").write_text(lock(hono="4.13.1", jose="6.2.8"))
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "baseline")
    return directory


def stub_resolve(monkeypatch, produced: str | None, *, returncode: int = 0):
    """Stand in for npm. Asserts the lock was removed before the resolve ran."""
    seen: dict[str, bool] = {}

    def fake(project_dir: Path, cutoff) -> subprocess.CompletedProcess[str]:
        lock_path = project_dir / npm.LOCKFILE
        # THE invariant. `--before` against a lock that already satisfies
        # package.json exits clean and changes nothing, so a resolve that runs
        # with the old lock still there bounds nothing and reports success.
        seen["removed"] = not lock_path.exists()
        if produced is not None:
            lock_path.write_text(produced)
        return subprocess.CompletedProcess(["npm"], returncode, "", "boom")

    monkeypatch.setattr(npm, "bounded_resolve", fake)
    return seen


def run(project_dir: Path, ref: str = "HEAD") -> int:
    return npm.main(
        ["--window", "P3D", "--baseline-ref", ref, "--project-dir", str(project_dir)]
    )


class TestSemverKey:
    """Ordering, and specifically the cases PEP 440 gets wrong.

    The uv driver compares with `packaging.Version`; reusing it here would be the
    obvious economy and is the bug this class exists to prevent.
    """

    @pytest.mark.parametrize(
        "lower, higher",
        [
            ("1.2.3", "1.2.4"),
            ("1.2.3", "1.3.0"),
            ("1.9.0", "1.10.0"),
            ("4.13.2", "4.13.4"),
            # A release outranks every prerelease of itself.
            ("1.0.0-rc.1", "1.0.0"),
            # Numeric identifiers compare numerically, not as strings.
            ("1.0.0-beta.2", "1.0.0-beta.11"),
            # A shorter run of identifiers ranks below a longer one sharing it.
            ("1.0.0-alpha", "1.0.0-alpha.1"),
            # Numeric identifiers rank below alphanumeric ones.
            ("1.0.0-1", "1.0.0-alpha"),
            ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ],
    )
    def test_ordering(self, lower: str, higher: str):
        assert npm.semver_key(lower) < npm.semver_key(higher)

    def test_a_numeric_prerelease_ranks_below_its_release(self):
        """PEP 440 reads `1.0.0-1` as `1.0.0.post1` and ranks it ABOVE `1.0.0`.

        Measured on packaging 26.3. Comparing that way turns a rollback from a
        release to one of its prereleases into an apparent upgrade, which the gate
        would then wave through — the single most consequential difference between
        the two schemes for this driver's purpose.
        """
        assert npm.semver_key("1.0.0-1") < npm.semver_key("1.0.0")

    @pytest.mark.parametrize(
        "version", ["7.0.0-next.5", "1.0.0-alpha.beta", "1.2.3-0.3.7"]
    )
    def test_prerelease_channels_pep440_rejects_still_parse(self, version: str):
        """All three raise InvalidVersion under packaging, and `-next.N` is an
        ordinary npm channel. Under the uv driver's key these would read as
        "cannot compare", which `regressions` reports — wedging the file into
        declining every single run for as long as one was in the tree."""
        assert npm.semver_key(version) is not None

    def test_build_metadata_is_ignored_as_semver_requires(self):
        # PEP 440 would read `+build.1` as a local version and rank it higher.
        assert npm.semver_key("1.0.0+build.1") == npm.semver_key("1.0.0")

    @pytest.mark.parametrize(
        "version", ["", "latest", "1.2", "not-a-version", "1.2.3.4"]
    )
    def test_unparseable_returns_none_rather_than_guessing(self, version: str):
        assert npm.semver_key(version) is None


class TestPackageName:
    def test_the_root_entry_has_no_package_name(self):
        assert npm.package_name("", {"name": "remediation"}) is None

    def test_a_scope_is_part_of_the_name(self):
        assert (
            npm.package_name("node_modules/@openprose/reactor", {})
            == "@openprose/reactor"
        )

    def test_a_nested_copy_resolves_to_the_same_name_as_a_hoisted_one(self):
        """Which is the whole point: three `content-type` copies at three depths
        are one package for comparison purposes."""
        nested = npm.package_name(
            "node_modules/negotiator/node_modules/content-type", {}
        )
        assert nested == npm.package_name("node_modules/content-type", {})
        assert nested == "content-type"


class TestLockVersions:
    def test_every_version_of_a_name_is_collected(self):
        versions = npm.lock_versions(
            lock_at(
                {
                    "node_modules/content-type": "2.1.0",
                    "node_modules/negotiator/node_modules/content-type": "2.0.0",
                }
            )
        )
        assert versions["content-type"] == {"2.1.0", "2.0.0"}

    def test_a_lockfile_with_no_packages_table_raises(self):
        """Rather than reading as "no packages", which would make every
        comparison against it vacuous and every resolve look clean."""
        with pytest.raises(ValueError, match="no `packages` table"):
            npm.lock_versions(json.dumps({"lockfileVersion": 1}))


class TestRegressions:
    def test_a_downgrade_is_caught(self):
        found = npm.regressions({"jose": {"6.2.10"}}, {"jose": {"6.2.9"}})
        assert found == {"jose": ("6.2.10", "6.2.9")}

    def test_an_upgrade_is_not(self):
        assert npm.regressions({"jose": {"6.2.8"}}, {"jose": {"6.2.10"}}) == {}

    def test_a_tree_reshuffle_at_the_same_versions_is_not_a_regression(self):
        """The versions are identical; only the install paths moved. Comparing by
        path would report one entry vanishing and another appearing, and this gate
        would decline the file forever on nothing at all."""
        before = npm.lock_versions(lock_at({"node_modules/content-type": "2.1.0"}))
        after = npm.lock_versions(
            lock_at({"node_modules/body-parser/node_modules/content-type": "2.1.0"})
        )
        assert npm.regressions(before, after) == {}

    def test_a_package_that_drops_out_entirely_is_not_a_regression(self):
        """Nothing depends on it in the bounded solution. Whatever caused it to
        drop out is itself a version move, and is reported under its own name."""
        assert npm.regressions({"gone": {"1.0.0"}}, {}) == {}

    def test_a_second_older_copy_appearing_is_not_a_regression(self):
        # main has one copy at 2.1.0; the resolve has 2.1.0 plus a nested 2.0.0.
        # Nothing was taken away, so nothing regressed.
        assert npm.regressions({"c": {"2.1.0"}}, {"c": {"2.1.0", "2.0.0"}}) == {}

    def test_losing_the_newest_of_several_copies_is_a_regression(self):
        assert npm.regressions({"c": {"2.1.0", "2.0.0"}}, {"c": {"2.0.0"}}) == {
            "c": ("2.1.0", "2.0.0")
        }

    def test_an_unparseable_version_is_reported_rather_than_skipped(self):
        found = npm.regressions({"weird": {"1.0.0"}}, {"weird": {"file:../local"}})
        assert "weird" in found

    def test_an_unparseable_version_both_sides_share_is_not_a_regression(self):
        """A git or file dependency pinned identically on both sides has not moved.
        Reporting it would decline every run forever — a wedge, not a control."""
        pinned = {"weird": {"file:../local"}}
        assert npm.regressions(pinned, pinned) == {}

    def test_a_set_with_one_unorderable_member_has_no_newest(self):
        # Picking the highest of the rest would compare against a version that may
        # not be the real ceiling.
        assert npm.newest({"1.0.0", "not-a-version"}) is None
        assert npm.newest({"1.0.0", "1.10.0", "1.9.0"}) == "1.10.0"


class TestDecline:
    def test_a_regressing_resolve_restores_the_baseline_byte_for_byte(
        self, project: Path, monkeypatch
    ):
        """The mechanism's whole promise. Not "restores equivalent versions" —
        restores the exact bytes the base branch ships, so the lane's diff for
        this file is empty and no rollback is possible even in principle.
        """
        baseline = npm.committed_text(project, "HEAD", npm.LOCKFILE)
        # What Renovate left on the branch: the unbounded refresh.
        (project / npm.LOCKFILE).write_text(lock(hono="4.13.4", jose="6.2.10"))
        # What a bounded resolve produces today — older than main on both, because
        # main adopted both inside the window before the bound existed.
        stub_resolve(
            monkeypatch,
            lock(hono="4.13.0", jose="6.2.7"),
        )

        assert run(project) == 0
        assert (project / npm.LOCKFILE).read_text() == baseline

    def test_a_decline_exits_zero(self, project: Path, monkeypatch):
        """A decline is an ordinary outcome, not an error: nothing in CI installs
        this lock, so there is no required check for a red to hold, and the state
        it leaves is exactly the safe one. Exiting non-zero here would stop
        bound_lock_branch committing the uv bounds that DID apply."""
        stub_resolve(monkeypatch, lock(hono="4.13.0"))
        assert run(project) == 0


class TestAdopt:
    def test_a_clean_resolve_is_kept(self, project: Path, monkeypatch):
        produced = lock(hono="4.13.2", jose="6.2.9")
        stub_resolve(monkeypatch, produced)
        assert run(project) == 0
        assert (project / npm.LOCKFILE).read_text() == produced

    def test_the_resolve_runs_against_no_lock_at_all(self, project: Path, monkeypatch):
        seen = stub_resolve(monkeypatch, lock(hono="4.13.2"))
        assert run(project) == 0
        assert seen["removed"], (
            "npm was handed the old lock, and `--before` against a lock that "
            "already satisfies package.json is a silent no-op"
        )

    def test_a_dropped_package_does_not_block_adoption(
        self, project: Path, monkeypatch
    ):
        stub_resolve(monkeypatch, lock(hono="4.13.2"))
        assert run(project) == 0
        assert "jose" not in (project / npm.LOCKFILE).read_text()


class TestFailClosed:
    def test_a_failed_resolve_restores_the_baseline_and_reports(
        self, project: Path, monkeypatch
    ):
        """Never Renovate's unbounded lock, which is what the worktree holds when
        this starts."""
        baseline = (project / npm.LOCKFILE).read_text()
        (project / npm.LOCKFILE).write_text(lock(hono="4.13.4"))
        stub_resolve(monkeypatch, None, returncode=1)

        assert run(project) == 1
        assert (project / npm.LOCKFILE).read_text() == baseline

    def test_an_unreadable_resolve_output_restores_the_baseline(
        self, project: Path, monkeypatch
    ):
        baseline = (project / npm.LOCKFILE).read_text()
        stub_resolve(monkeypatch, "{ this is not json")
        assert run(project) == 1
        assert (project / npm.LOCKFILE).read_text() == baseline

    def test_a_manifest_that_moved_refuses_rather_than_restoring(
        self, project: Path, monkeypatch
    ):
        """A restore is only coherent while the manifest the baseline was resolved
        from is still the manifest on the branch — otherwise it writes a lock that
        `npm ci` rejects. lockFileMaintenance never edits a manifest, so this
        should not fire; the guard is here because the corruption would be silent.
        """
        refreshed = lock(hono="4.13.4")
        (project / npm.LOCKFILE).write_text(refreshed)
        (project / "package.json").write_text(
            json.dumps(
                {**MANIFEST, "devDependencies": {"@openprose/reactor": "^0.4.0"}}
            )
        )
        called: list[bool] = []
        monkeypatch.setattr(
            npm, "bounded_resolve", lambda *a, **k: called.append(True) or None
        )

        assert run(project) == 1
        assert called == [], "the resolve must not run when there is no safe fallback"
        assert (project / npm.LOCKFILE).read_text() == refreshed

    def test_an_unparseable_window_fails_before_touching_anything(
        self, project: Path, monkeypatch
    ):
        baseline = (project / npm.LOCKFILE).read_text()
        monkeypatch.setattr(
            npm, "bounded_resolve", lambda *a, **k: pytest.fail("should not resolve")
        )
        assert (
            npm.main(
                [
                    "--window",
                    "7 days",
                    "--baseline-ref",
                    "HEAD",
                    "--project-dir",
                    str(project),
                ]
            )
            == 1
        )
        assert (project / npm.LOCKFILE).read_text() == baseline


class TestNoBaseline:
    def test_a_lock_added_in_this_branch_is_left_alone(
        self, project: Path, monkeypatch
    ):
        """No baseline means no gate and nothing to fall back to, and a bounded
        resolve with no gate behind it is the mass-rollback shape this exists to
        avoid."""
        git(project, "rm", "-q", "--cached", npm.LOCKFILE)
        git(project, "commit", "-qm", "drop the lock")
        added = lock(hono="4.13.4")
        (project / npm.LOCKFILE).write_text(added)
        monkeypatch.setattr(
            npm, "bounded_resolve", lambda *a, **k: pytest.fail("should not resolve")
        )

        assert run(project) == 0
        assert (project / npm.LOCKFILE).read_text() == added

    def test_a_project_without_the_pair_is_a_clean_no_op(self, tmp_path: Path):
        assert run(tmp_path) == 0


class TestCommittedText:
    def test_a_nested_project_reads_its_own_lock_and_not_the_repo_roots(
        self, project: Path
    ):
        """`git show <rev>:<path>` is repo-root-relative wherever it runs, so
        without the `./` prefix this returns the ROOT lock as the nested project's
        baseline — a comparison against a completely unrelated file."""
        text = npm.committed_text(project, "HEAD", npm.LOCKFILE)
        assert text is not None
        assert "decoy" not in text
        assert "hono" in text

    def test_an_absent_path_is_none_and_a_broken_ref_raises(self, project: Path):
        assert npm.committed_text(project, "HEAD", "no-such-file.json") is None
        with pytest.raises(RuntimeError):
            npm.committed_text(project, "refs/heads/nope", npm.LOCKFILE)


class TestWorkflowWiring:
    """The npm lock has to be in the trigger's `paths:`, and node has to be on the
    runner. Both fail by the job not running or the step not finding npm, and
    neither is red anywhere that matters — the check is not required."""

    @property
    def job(self) -> dict:
        return yaml.safe_load(WORKFLOW.read_text())["jobs"]["bound"]

    def test_the_npm_lock_triggers_the_lane(self):
        # Measured on #3355: the branch's entire net diff after the uv bound was
        # this one file, so a push carrying only it must still run the job.
        triggers = yaml.safe_load(WORKFLOW.read_text())[True]
        assert (
            "packages/conformance/conformance/package-lock.json"
            in triggers["push"]["paths"]
        )

    def test_node_is_pinned_rather_than_taken_from_the_runner_image(self):
        # npm's major decides the lockfileVersion it writes; a runner rolling
        # forward to one that writes a different version would turn every bounded
        # resolve into a whole-file rewrite.
        setup = next(
            step
            for step in self.job["steps"]
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        )
        assert setup["uses"].split("@")[1].split()[0], "setup-node must be SHA-pinned"
        assert setup["with"]["node-version"] == "24"

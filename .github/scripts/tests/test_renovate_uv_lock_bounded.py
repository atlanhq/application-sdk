"""Tests for .github/scripts/renovate_uv_lock_bounded.py.

Pure-unit: no network, no uv invocation. The uv call itself is exercised
end-to-end by the pilot repo's CI, not here — what needs regression cover is the
logic around it, which is where both prior attempts failed (a lockfile that
`uv sync --locked` rejected, and a bound that vanished without anything going
red).
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_uv_lock_bounded as bounded

# A lock in the shape uv writes it, including the [options] block that broke the
# fleet in FND-367 and the [options.exclude-newer-package] subtable beside it.
LOCK_WITH_OPTIONS = """\
version = 1
revision = 3
requires-python = ">=3.11"

[options]
exclude-newer = "0001-01-01T00:00:00Z" # This has no effect and is included for backwards compatibility when using relative exclude-newer values.
exclude-newer-span = "P7D"

[options.exclude-newer-package]
atlan-application-sdk = { timestamp = "0001-01-01T00:00:00Z", span = "PT0S" }
pyatlan = { timestamp = "0001-01-01T00:00:00Z", span = "PT0S" }

[[package]]
name = "atlan-application-sdk"
version = "3.28.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "boto3"
version = "1.43.67"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "hello-world-app"
version = "0.1.0"
source = { editable = "." }
"""


def with_options(lock_text: str, window: str = "P3D") -> str:
    """`lock_text` as uv leaves it after a bounded resolve: same versions, plus
    the [options] table recording the window."""
    table = f'\n[options]\nexclude-newer-span = "{window}"\n'
    anchor = lock_text.find("\n[[package]]")
    if anchor == -1:
        return lock_text + table
    return lock_text[:anchor] + table + lock_text[anchor:]


def lock(**packages: str) -> str:
    body = 'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n'
    for name, version in packages.items():
        body += (
            f'\n[[package]]\nname = "{name.replace("_", "-")}"\n'
            f'version = "{version}"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
        )
    return body


class TestStripOptions:
    def test_removes_options_table_and_its_subtable(self):
        stripped = bounded.strip_options(LOCK_WITH_OPTIONS)
        assert "[options]" not in stripped
        assert "exclude-newer" not in stripped
        assert "[options.exclude-newer-package]" not in stripped

    def test_keeps_every_package_and_the_header(self):
        stripped = bounded.strip_options(LOCK_WITH_OPTIONS)
        assert stripped.count("[[package]]") == 3
        assert 'name = "atlan-application-sdk"' in stripped
        assert 'version = "3.28.0"' in stripped
        assert "requires-python" in stripped

    def test_result_is_parseable_and_keeps_versions(self):
        versions = bounded.lock_versions(bounded.strip_options(LOCK_WITH_OPTIONS))
        assert versions["atlan-application-sdk"] == "3.28.0"
        assert versions["boto3"] == "1.43.67"

    def test_lock_without_options_is_left_byte_identical(self):
        # The common case once a repo is on the bounded lane but a refresh
        # produced no [options] — the strip must not churn the file.
        original = lock(boto3="1.43.67")
        assert bounded.strip_options(original) == original


class TestLockVersions:
    def test_skips_non_registry_sources(self):
        # The workspace member itself has no registry version to compare.
        assert "hello-world-app" not in bounded.lock_versions(LOCK_WITH_OPTIONS)

    def test_normalises_names(self):
        assert bounded.lock_versions(lock(Typing_Inspection="0.4.2")) == {
            "typing-inspection": "0.4.2"
        }

    def test_malformed_toml_is_empty_not_an_exception(self):
        assert bounded.lock_versions("[[package]\nname = ") == {}


class TestFlooredPackages:
    def test_reads_constraint_dependencies_and_project_dependencies(self):
        pyproject = """\
[project]
name = "app"
dependencies = ["atlan-application-sdk>=3.27.2", "orjson"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.uv]
constraint-dependencies = ["cryptography>=46.0.5", "protobuf>=6.33.5"]
"""
        # "orjson" is a bare name with no version specifier, so it is NOT a floor.
        assert bounded.floored_packages(pyproject) == {
            "atlan-application-sdk",
            "pytest",
            "cryptography",
            "protobuf",
        }

    def test_bare_name_without_a_specifier_is_not_a_floor(self):
        # A requirement with no version constraint pins nothing, so it must not be
        # treated as a deliberate floor the cooldown should yield to.
        pyproject = """\
[project]
name = "app"
dependencies = ["orjson", "requests"]
"""
        assert bounded.floored_packages(pyproject) == set()

    def test_exact_pin_counts_as_a_floor(self):
        pyproject = """\
[project]
name = "app"
dependencies = ["orjson==3.10.7", "urllib3~=2.2"]
"""
        # "==" is an exact pin (a floor); "~=" is a compatible-release range, not
        # an explicit lower-bound/exact pin, so it is not treated as a floor.
        assert bounded.floored_packages(pyproject) == {"orjson"}

    def test_marker_only_specifier_is_not_a_floor(self):
        # The `>=` here constrains the interpreter, not the package, so a
        # floor-less dependency carrying only an environment marker must not be
        # classified as floored (and so must not earn a P0D retry exemption).
        pyproject = """\
[project]
name = "app"
dependencies = ["foo; python_version >= \\"3.10\\"", "bar>=1.0; python_version >= \\"3.10\\""]
"""
        assert bounded.floored_packages(pyproject) == {"bar"}

    def test_direct_reference_url_is_not_a_floor(self):
        # The `==`/`>=` here live in the URL, not the version specifier, so a
        # direct-reference dependency must not be misread as floored. The PEP 508
        # parse separates the URL from the (empty) specifier.
        pyproject = """\
[project]
name = "app"
dependencies = [
    "foo @ https://example.example/pkg.tar.gz?constraint==1",
    "bar @ https://example.example/bar.whl ; python_version >= \\"3.10\\"",
    "baz>=2.0",
]
"""
        assert bounded.floored_packages(pyproject) == {"baz"}

    def test_malformed_pyproject_is_empty_not_an_exception(self):
        assert bounded.floored_packages("[project\nname =") == set()


class TestDeclaresOwnBound:
    def test_detects_either_key(self):
        assert bounded.declares_own_bound('[tool.uv]\nexclude-newer = "P7D"\n')
        assert bounded.declares_own_bound(
            '[tool.uv]\nexclude-newer-package = { pyatlan = "P0D" }\n'
        )

    def test_absent_or_unrelated_tool_uv_is_not_a_bound(self):
        assert not bounded.declares_own_bound("[project]\nname = 'app'\n")
        assert not bounded.declares_own_bound(
            '[tool.uv]\ndefault-groups = ["dev"]\nconstraint-dependencies = ["x>=1"]\n'
        )

    def test_malformed_pyproject_is_not_treated_as_a_bound(self):
        assert not bounded.declares_own_bound("[tool.uv\nexclude-newer =")


class TestBlockedByFloor:
    def test_intersects_the_error_with_deliberate_floors_only(self):
        stderr = (
            "error: No solution found when resolving dependencies:\n"
            "  Because only cryptography<=46.0.4 is available and your project "
            "requires cryptography>=46.0.5, we can conclude that ...\n"
        )
        floors = {"cryptography", "protobuf"}
        assert bounded.blocked_by_floor(stderr, floors) == ["cryptography"]

    def test_ignores_packages_the_repo_never_floored(self):
        # uv names plenty of packages in a resolution trace; only floored ones
        # may be admitted early, so an unrelated mention can never widen the bound.
        stderr = "Because only boto3<=1.43.60 is available ..."
        assert bounded.blocked_by_floor(stderr, {"cryptography"}) == []

    def test_no_floors_means_nothing_to_admit(self):
        assert bounded.blocked_by_floor("Because only cryptography...", set()) == []


class TestRollbacks:
    def test_flags_an_exempt_package_resolved_backwards(self):
        # The observed failure: SDK 3.28.0 needs pyatlan>=10, pyatlan 10 is inside
        # the window, so uv backtracks to 3.27.2 with no error.
        found = bounded.rollbacks(
            {"atlan-application-sdk": "3.28.0"},
            {"atlan-application-sdk": "3.27.2"},
            ["atlan-application-sdk"],
        )
        assert found == {"atlan-application-sdk": ("3.28.0", "3.27.2")}

    def test_flags_third_party_moving_backwards(self):
        # This test previously asserted the opposite — that a third-party
        # downgrade was "the bound working as intended". It was not: measured on
        # the pilot, a plain bounded resolve rolled 13 packages back against
        # hello-world's main (boto3 1.43.72 -> 1.43.67, starlette 1.6.0 -> 1.4.1,
        # ...) with zero upgrades. Reverting a version that was adopted because it
        # fixed something is a worse outcome than adopting a fix seven days late,
        # so retention ceilings prevent it and this check catches any that escape.
        assert bounded.rollbacks({"boto3": "1.43.72"}, {"boto3": "1.43.67"}) == {
            "boto3": ("1.43.72", "1.43.67")
        }

    def test_scoping_to_named_packages_still_works(self):
        found = bounded.rollbacks(
            {"boto3": "1.43.72", "starlette": "1.6.0"},
            {"boto3": "1.43.67", "starlette": "1.4.1"},
            ["boto3"],
        )
        assert found == {"boto3": ("1.43.72", "1.43.67")}

    def test_unchanged_and_forward_moves_are_clean(self):
        before = {"pyatlan": "10.0.0", "atlan-application-sdk": "3.27.2"}
        after = {"pyatlan": "10.0.0", "atlan-application-sdk": "3.28.0"}
        assert (
            bounded.rollbacks(before, after, ["pyatlan", "atlan-application-sdk"]) == {}
        )

    def test_unparseable_version_is_reported_rather_than_ignored(self):
        found = bounded.rollbacks(
            {"pyatlan": "10.0.0"}, {"pyatlan": "not-a-version"}, ["pyatlan"]
        )
        assert "pyatlan" in found

    def test_missing_on_either_side_is_not_a_rollback(self):
        assert bounded.rollbacks({}, {"pyatlan": "10.0.0"}, ["pyatlan"]) == {}
        assert bounded.rollbacks({"pyatlan": "10.0.0"}, {}, ["pyatlan"]) == {}

    def test_compares_numerically_not_lexically(self):
        # "1.43.9" > "1.43.67" as strings; as versions it is a rollback.
        assert bounded.rollbacks(
            {"boto3": "1.43.67"}, {"boto3": "1.43.9"}, ["boto3"]
        ) == {"boto3": ("1.43.67", "1.43.9")}

    def test_flags_a_prerelease_rollback(self):
        # A leading-digits comparison reads both as 0.62 and misses this; PEP 440
        # orders a beta before its release, so 0.62b2 -> 0.62b1 is a rollback.
        assert bounded.rollbacks({"a": "0.62b2"}, {"a": "0.62b1"}) == {
            "a": ("0.62b2", "0.62b1")
        }

    def test_flags_an_epoch_rollback(self):
        # The epoch (1!) outranks every version without one, so dropping it is a
        # rollback even though 2.0 > 1.0 numerically.
        assert bounded.rollbacks({"a": "1!2.0"}, {"a": "2.0"}) == {
            "a": ("1!2.0", "2.0")
        }

    def test_a_forward_prerelease_move_is_clean(self):
        # 0.62b1 -> 0.62b2 moves forward and must not be flagged.
        assert bounded.rollbacks({"a": "0.62b1"}, {"a": "0.62b2"}) == {}


class TestParseWindow:
    def test_days_and_hours(self):
        assert bounded.parse_window("P7D") == datetime.timedelta(days=7)
        assert bounded.parse_window("PT12H") == datetime.timedelta(hours=12)
        assert bounded.parse_window("P1DT6H") == datetime.timedelta(days=1, hours=6)

    def test_calendar_units_and_junk_are_rejected_not_approximated(self):
        # A window that silently means something other than what it says is worse
        # than one that fails to parse.
        for bad in ["P1M", "P1Y", "7 days", "P", "", "PT"]:
            try:
                bounded.parse_window(bad)
            except ValueError:
                continue
            raise AssertionError(f"{bad!r} should not parse")


class TestLockUploadTimes:
    def test_takes_the_newest_timestamp_across_a_package_files(self):
        # Wheels for different platforms are published seconds apart; the ceiling
        # has to admit every file the resolver needs, so the newest one wins.
        text = """\
version = 1

[[package]]
name = "boto3"
version = "1.43.72"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://x/a.tar.gz", upload-time = "2026-08-14T19:24:48.841Z" }
wheels = [
    { url = "https://x/a.whl", upload-time = "2026-08-14T19:24:52.013Z" },
    { url = "https://x/b.whl", upload-time = "2026-08-14T19:24:53.290Z" },
]
"""
        times = bounded.lock_upload_times(text)
        assert times["boto3"] == datetime.datetime(
            2026, 8, 14, 19, 24, 53, 290000, tzinfo=datetime.timezone.utc
        )

    def test_package_without_timestamps_is_absent(self):
        assert bounded.lock_upload_times(lock(boto3="1.43.72")) == {}


class TestRetentionCeilings:
    CUTOFF = datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc)

    def test_package_older_than_the_cutoff_needs_no_ceiling(self):
        times = {"boto3": datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)}
        assert bounded.retention_ceilings(times, self.CUTOFF) == {}

    def test_package_inside_the_window_is_pinned_one_second_past_its_upload(self):
        # One second past, so uv can keep exactly that version and take nothing
        # newer.
        times = {
            "boto3": datetime.datetime(
                2026, 8, 14, 19, 24, 53, tzinfo=datetime.timezone.utc
            )
        }
        assert bounded.retention_ceilings(times, self.CUTOFF) == {
            "boto3": "2026-08-14T19:24:54Z"
        }


class TestBuildUvCommand:
    def test_retention_ceilings_become_per_package_flags(self):
        command = bounded.build_uv_command("P7D", [], {"boto3": "2026-08-14T19:24:54Z"})
        assert "--exclude-newer-package" in command
        assert "boto3=2026-08-14T19:24:54Z" in command

    def test_exemption_wins_over_a_ceiling_for_the_same_package(self):
        # A first-party package must be free to move FORWARD, not merely held
        # where it is — so it must not also carry a retention ceiling.
        command = bounded.build_uv_command(
            "P7D", ["pyatlan"], {"pyatlan": "2026-08-13T09:46:02Z"}
        )
        assert "pyatlan=P0D" in command
        assert "pyatlan=2026-08-13T09:46:02Z" not in command

    def test_bound_and_one_exemption_flag_per_package(self):
        assert bounded.build_uv_command(
            "P7D", ["atlan-application-sdk", "pyatlan"]
        ) == [
            "uv",
            "lock",
            "--upgrade",
            "--exclude-newer",
            "P7D",
            "--exclude-newer-package",
            "atlan-application-sdk=P0D",
            "--exclude-newer-package",
            "pyatlan=P0D",
        ]

    def test_bound_is_always_present_even_with_no_exemptions(self):
        command = bounded.build_uv_command("P7D", [])
        assert "--exclude-newer" in command
        assert "--exclude-newer-package" not in command


class TestMain:
    """The orchestration, with uv stubbed. What matters here is that no path
    reaches an unbounded lockfile without a non-zero exit."""

    def _project(self, tmp_path: Path, lock_text: str, pyproject: str = "") -> Path:
        """A real git repo with the lock committed.

        Committing matters: the driver reads its baseline from `HEAD:uv.lock`,
        not the working tree, and a fixture that skipped the commit would let a
        regression in that distinction pass unnoticed — while in production it
        would neutralise the whole bound.
        """
        (tmp_path / "uv.lock").write_text(lock_text)
        (tmp_path / "pyproject.toml").write_text(
            pyproject or "[project]\nname = 'app'\n"
        )
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": os.environ.get("PATH", ""),
        }
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "baseline"],
        ):
            subprocess.run(command, cwd=tmp_path, check=True, env=env)
        return tmp_path

    def test_missing_lockfile_is_a_clean_no_op(self, tmp_path):
        assert bounded.main(["--window", "P7D", "--project-dir", str(tmp_path)]) == 0

    def test_success_strips_options_from_the_committed_lock(
        self, monkeypatch, tmp_path
    ):
        # Baseline matches what the stubbed resolve returns, so this test
        # exercises the strip alone — a baseline of 1.43.72 would now (correctly)
        # trip the rollback check instead.
        project = self._project(tmp_path, lock(boto3="1.43.67"))

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(LOCK_WITH_OPTIONS)
            return __import__("subprocess").CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 0
        assert "[options]" not in (project / "uv.lock").read_text()

    def test_unsatisfiable_floor_retries_once_admitting_that_package(
        self, monkeypatch, tmp_path
    ):
        project = self._project(
            tmp_path,
            lock(cryptography="46.0.5"),
            "[project]\nname = 'app'\n\n[tool.uv]\n"
            'constraint-dependencies = ["cryptography>=46.0.5"]\n',
        )
        calls: list[list[str]] = []

        def fake_run(command, cwd):
            calls.append(command)
            subprocess = __import__("subprocess")
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    "",
                    "error: No solution found ... cryptography>=46.0.5 ...",
                )
            (Path(cwd) / "uv.lock").write_text(lock(cryptography="46.0.5"))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 0
        assert len(calls) == 2
        assert "cryptography=P0D" in calls[1]

    def test_failure_with_no_floor_named_does_not_retry_and_fails(
        self, monkeypatch, tmp_path
    ):
        project = self._project(tmp_path, lock(boto3="1.43.72"))
        calls: list[list[str]] = []

        def fake_run(command, cwd):
            calls.append(command)
            return __import__("subprocess").CompletedProcess(
                command, 1, "", "error: something else entirely"
            )

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 1
        assert len(calls) == 1, "must not retry blind, and must never resolve unbounded"

    def test_bare_dep_named_in_error_does_not_get_a_p0d_exemption(
        self, monkeypatch, tmp_path
    ):
        """The fail-open seam from the review: a bare dependency (no version
        specifier) is not a floor, so its mere mention in uv's stderr must NOT earn
        it a P0D exemption that bypasses the window. The driver must fail rather
        than retry with the bound relaxed for that package."""
        project = self._project(
            tmp_path,
            lock(orjson="3.10.7"),
            "[project]\nname = 'app'\ndependencies = ['orjson']\n",
        )
        calls: list[list[str]] = []

        def fake_run(command, cwd):
            calls.append(command)
            return __import__("subprocess").CompletedProcess(
                command, 1, "", "error: No solution found ... orjson ..."
            )

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 1
        assert len(calls) == 1, "a bare dep is not a floor, so no P0D retry"
        assert not any(
            "orjson=P0D" in part for command in calls for part in command
        ), "orjson must never be exempted inside the window"

    def test_ceilings_come_from_head_not_the_working_tree(self, monkeypatch, tmp_path):
        """The single most important property in this file.

        By the time this driver runs, Renovate has already rewritten the working
        tree's uv.lock to latest-of-everything. If the baseline were read from
        there, every one of those fresh releases would get a retention ceiling
        pinning it in place, and the release-age window would be worth nothing —
        while every check still passed. So: commit an OLD lock, dirty the working
        tree with a FRESH one, and assert the ceiling names the old version.
        """
        old_upload = "2020-01-01T00:00:00Z"
        committed = (
            'version = 1\n\n[[package]]\nname = "boto3"\nversion = "1.0.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'sdist = {{ url = "https://x/a.tar.gz", upload-time = "{old_upload}" }}\n'
        )
        project = self._project(tmp_path, committed)

        fresh_upload = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        (project / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "boto3"\nversion = "9.9.9"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'sdist = {{ url = "https://x/z.tar.gz", upload-time = "{fresh_upload}" }}\n'
        )

        seen: list[list[str]] = []

        def fake_run(command, cwd):
            seen.append(command)
            (Path(cwd) / "uv.lock").write_text(committed)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        # Exit 1, not 0: this fixture is also the hold-everything case — the bound
        # admits nothing while Renovate's working-tree copy moved boto3 — so the
        # run withholds rather than hand that copy to the branch. What this test
        # is about is the ceiling flags below.
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 1

        # boto3's committed version is from 2020, comfortably outside the window,
        # so it needs no ceiling. Had the baseline been the working tree, the
        # hour-old 9.9.9 would have produced one.
        flags = [a for a in seen[0] if a.startswith("boto3=")]
        assert flags == [], f"unexpected ceiling from the working tree: {flags}"

    def test_ceiling_is_emitted_for_a_recently_adopted_package(
        self, monkeypatch, tmp_path
    ):
        recent = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        committed = (
            'version = 1\n\n[[package]]\nname = "boto3"\nversion = "1.43.72"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'sdist = {{ url = "https://x/a.tar.gz", upload-time = "{recent}" }}\n'
        )
        project = self._project(tmp_path, committed)
        seen: list[list[str]] = []

        def fake_run(command, cwd):
            seen.append(command)
            (Path(cwd) / "uv.lock").write_text(committed)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 0
        assert any(a.startswith("boto3=2") for a in seen[0]), seen[0]

    def test_unresolvable_head_fails_closed(self, tmp_path):
        # Not a git repo: we cannot tell what was previously adopted, and
        # proceeding without ceilings would silently reintroduce rollbacks.
        (tmp_path / "uv.lock").write_text(lock(boto3="1.43.72"))
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        assert bounded.main(["--window", "P7D", "--project-dir", str(tmp_path)]) == 1

    def test_unreadable_head_lock_blob_fails_closed(self, monkeypatch, tmp_path):
        # HEAD resolves, but `git show HEAD:uv.lock` fails for a reason other
        # than "path absent" (a corrupt/missing blob). Treating that as "no
        # baseline" would silently drop the retention ceilings and the rollback
        # comparison — the one fail-open path left in the driver — so the run
        # must fail closed and never reach uv.
        project = self._project(tmp_path, lock(boto3="1.43.72"))
        real_run = subprocess.run

        def fake_git_run(command, **kwargs):
            if command[:2] == ["git", "show"]:
                return subprocess.CompletedProcess(
                    command, 128, "", "fatal: bad object HEAD:uv.lock"
                )
            return real_run(command, **kwargs)

        def fail_if_called(command, cwd):
            raise AssertionError("uv must not run when the baseline is unreadable")

        monkeypatch.setattr(bounded.subprocess, "run", fake_git_run)
        monkeypatch.setattr(bounded, "run_uv_lock", fail_if_called)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 1

    def test_lock_absent_from_head_returns_none_and_proceeds(
        self, monkeypatch, tmp_path
    ):
        # A lock added in this very branch: HEAD has no committed copy, so the
        # driver proceeds with no ceilings and no rollback comparison (the
        # documented new-lockfile path). The fail-closed rework must NOT turn
        # this into a failure — absence is established by `git ls-tree`
        # succeeding empty, not by `git show` failing (which it also does for an
        # unreadable blob).
        project = self._project(tmp_path, lock(boto3="1.43.72"))
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "rm", "-q", "uv.lock"], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "drop the lock"], cwd=project, check=True, env=env
        )
        (project / "uv.lock").write_text(lock(boto3="1.43.72"))

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(lock(boto3="1.43.72"))
            return __import__("subprocess").CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 0
        # Absence is verifiable: the tree probe is what returned None.
        assert bounded.baseline_lock_text(project) is None

    def test_repo_with_its_own_bound_is_left_completely_alone(
        self, monkeypatch, tmp_path
    ):
        # glue, bw, thoughtspot and dbt predate the fleet mechanism. Their lock
        # already carries [options] matching their pyproject, so stripping it
        # would break `uv sync --locked` in their image build — FND-367 from the
        # other direction. Skip, do not touch, and say so.
        original = lock(boto3="1.43.72")
        project = self._project(
            tmp_path,
            original,
            "[project]\nname = 'app'\n\n[tool.uv]\nexclude-newer = \"7 days\"\n",
        )

        def fail_if_called(command, cwd):
            raise AssertionError("uv must not run against a repo with its own bound")

        monkeypatch.setattr(bounded, "run_uv_lock", fail_if_called)
        assert bounded.main(["--window", "P7D", "--project-dir", str(project)]) == 0
        assert (project / "uv.lock").read_text() == original

    def test_rejects_an_unparseable_window_before_running_uv(self, tmp_path):
        project = self._project(tmp_path, lock(boto3="1.43.72"))
        assert bounded.main(["--window", "P1M", "--project-dir", str(project)]) == 1

    def test_silent_rollback_of_an_exempt_package_fails_the_run(
        self, monkeypatch, tmp_path
    ):
        project = self._project(tmp_path, lock(atlan_application_sdk="3.28.0"))

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(lock(atlan_application_sdk="3.27.2"))
            return __import__("subprocess").CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        exit_code = bounded.main(
            [
                "--window",
                "P7D",
                "--exempt",
                "atlan-application-sdk",
                "--project-dir",
                str(project),
            ]
        )
        assert exit_code == 1


class TestBaselineRef:
    """`--baseline-ref`, and the sub-project path fix beside it (FND-376).

    Under Renovate's postUpgradeTasks the refresh is still uncommitted, so HEAD
    is the pre-refresh lock and the default is right. application-sdk's own lane
    runs from a workflow triggered by the push Renovate already made, so HEAD
    there IS the unbounded lock — the baseline has to come from the base branch,
    or the window is worth nothing while every check still passes.
    """

    GIT_ENV = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": os.environ.get("PATH", ""),
    }

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, env=self.GIT_ENV)

    def _package(self, name: str, version: str, uploaded: str) -> str:
        return (
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'sdist = {{ url = "https://x/{name}-{version}.tar.gz", '
            f'upload-time = "{uploaded}" }}\n'
        )

    def _stamp(self, **delta: int) -> str:
        moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            **delta
        )
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_head_as_baseline_neutralises_the_window_and_the_base_ref_does_not(
        self, monkeypatch, tmp_path
    ):
        """The property the flag exists for, asserted against its own control.

        Two commits: the base locks a 2020 boto3, then the branch commits the
        refreshed lock holding an hour-old one. Read from HEAD, that fresh
        release looks *already adopted* and earns a retention ceiling — the bound
        pinning in place the very release it exists to exclude. Read from the base
        branch, it earns nothing.
        """
        old = "version = 1\n" + self._package("boto3", "1.0.0", "2020-01-01T00:00:00Z")
        fresh = "version = 1\n" + self._package("boto3", "9.9.9", self._stamp(hours=1))

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        (tmp_path / "uv.lock").write_text(old)
        self._git(tmp_path, "init", "-q")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-qm", "base")
        self._git(tmp_path, "branch", "base")

        # Renovate's commit: the unbounded refresh is now IN history, not merely
        # in the working tree. That is what makes this lane different.
        (tmp_path / "uv.lock").write_text(fresh)
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-qm", "chore(deps): lock file maintenance")

        seen: list[list[str]] = []
        # What the stubbed resolve leaves behind. Set per run to whatever that
        # run's baseline already holds, so the rollback gate stays out of the way
        # and the assertion is about the ceilings alone.
        resolved = old

        def fake_run(command, cwd):
            seen.append(command)
            (Path(cwd) / "uv.lock").write_text(resolved)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)

        argv = ["--window", "P7D", "--project-dir", str(tmp_path)]
        # Withheld (1), not clean (0): the bound admits nothing here while the
        # branch commit carries a fresh boto3, so the run refuses rather than
        # leave Renovate's copy in place. The ceilings are what is under test.
        assert bounded.main([*argv, "--baseline-ref", "base"]) == 1
        ceilings = [a for a in seen[0] if a.startswith("boto3=")]
        assert ceilings == [], (
            "the base branch locks a 2020 boto3, so nothing needs a ceiling; one "
            f"here means the fresh 9.9.9 was read as already adopted: {ceilings}"
        )

        # The control: the same tree on the default HEAD baseline DOES emit one.
        # Without this half the assertion above could pass for the wrong reason.
        seen.clear()
        resolved = fresh
        # Put the tree back the way Renovate leaves it, since the withheld run
        # above rewrote uv.lock. In production each run re-resolves from scratch;
        # here the fixture has to say so.
        (tmp_path / "uv.lock").write_text(fresh)
        assert bounded.main(argv) == 0
        assert any(a.startswith("boto3=2") for a in seen[0]), seen[0]

    def test_a_subproject_baseline_is_its_own_lock_not_the_repo_roots(
        self, monkeypatch, tmp_path
    ):
        """`<rev>:uv.lock` is repo-root-relative wherever it runs.

        So a run against `packages/conformance` took the ROOT lock as that
        project's baseline — silently, with unrelated retention ceilings and a
        rollback comparison against the wrong file. `<rev>:./uv.lock` is what makes
        `git show` agree with the `git ls-tree` probe beside it. Both locks name
        the same package at different ages, so a leak between them cannot pass
        unnoticed.
        """
        root_lock = "version = 1\n" + self._package(
            "boto3", "1.0.0", "2020-01-01T00:00:00Z"
        )
        # The sub-project's boto3 is recent, so it — and only it — earns a ceiling.
        sub_lock = "version = 1\n" + self._package(
            "boto3", "5.5.5", self._stamp(days=2)
        )

        sub = tmp_path / "packages" / "conformance"
        sub.mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        (tmp_path / "uv.lock").write_text(root_lock)
        (sub / "pyproject.toml").write_text("[project]\nname = 'sub'\n")
        (sub / "uv.lock").write_text(sub_lock)
        self._git(tmp_path, "init", "-q")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-qm", "base")

        assert bounded.baseline_lock_text(sub) == sub_lock
        assert bounded.baseline_lock_text(tmp_path) == root_lock

        seen: list[list[str]] = []

        def fake_run(command, cwd):
            seen.append(command)
            (Path(cwd) / "uv.lock").write_text(sub_lock)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P7D", "--project-dir", str(sub)]) == 0
        assert any(a.startswith("boto3=2") for a in seen[0]), (
            "the sub-project's boto3 is 2 days old and needs a ceiling; its "
            f"absence means the 2020 ROOT lock was read as the baseline: {seen[0]}"
        )

    def test_an_unresolvable_baseline_ref_fails_closed(self, monkeypatch, tmp_path):
        # A misspelled or unfetched base branch must not degrade to "no baseline",
        # which would drop the retention ceilings and the rollback gate together.
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        (tmp_path / "uv.lock").write_text(lock(boto3="1.43.72"))
        self._git(tmp_path, "init", "-q")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-qm", "base")

        def fail_if_called(command, cwd):
            raise AssertionError("uv must not run without a resolvable baseline")

        monkeypatch.setattr(bounded, "run_uv_lock", fail_if_called)
        exit_code = bounded.main(
            [
                "--window",
                "P7D",
                "--project-dir",
                str(tmp_path),
                "--baseline-ref",
                "origin/nope",
            ]
        )
        assert exit_code == 1


class TestWithholds:
    """A refusal has to land on a REQUIRED check, or it withholds nothing.

    Renovate captures its own unbounded resolve before this command runs, commits
    the working tree afterwards whatever the exit code, and falls back to that
    captured artifact whenever the tree matches HEAD. `renovate/artifacts` is
    advisory — not a required check in any lock-lane repo, and those repos need no
    approving review either — and Renovate re-arms GitHub-native automerge in the
    same pass that records the failure. So every refusal writes the baseline's
    versions plus an `[options]` table the repo does not declare: `uv sync
    --locked` rejects that, and `scan / Build Image` is required wherever this
    lane runs.
    """

    GIT_ENV = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": os.environ.get("PATH", ""),
    }

    def _repo(self, tmp_path: Path, lock_text: str, pyproject: str = "") -> Path:
        (tmp_path / "uv.lock").write_text(lock_text)
        (tmp_path / "pyproject.toml").write_text(
            pyproject or "[project]\nname = 'app'\n"
        )
        for command in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "baseline"],
        ):
            subprocess.run(command, cwd=tmp_path, check=True, env=self.GIT_ENV)
        return tmp_path

    def _assert_withheld(self, project: Path, baseline: str) -> None:
        """The three properties a refusal needs, together.

        The base branch's versions (so neither the rejected resolve nor Renovate's
        unbounded one lands), an `[options]` table (so a required check rejects
        it), and NOT byte-identical to the base branch — that last one is what
        stops Renovate substituting its own captured artifact.
        """
        on_disk = (project / "uv.lock").read_text()
        assert bounded.lock_versions(on_disk) == bounded.lock_versions(baseline)
        assert "[options]" in on_disk
        assert on_disk != baseline
        assert bounded.strip_options(on_disk) == baseline

    def test_the_bound_admitting_nothing_is_withheld_not_shipped(
        self, monkeypatch, tmp_path
    ):
        """The common case, and the one that was merging unattended.

        Renovate's unbounded pass moves a package published minutes ago and the
        bound correctly holds it. Restoring the baseline here would leave the tree
        matching HEAD, and Renovate would commit its own copy instead — a valid
        lock that passes every required check while carrying a minutes-old
        release.
        """
        baseline = lock(boto3="1.43.74")
        project = self._repo(tmp_path, baseline)
        # Renovate got here first: this is the artifact it has already captured.
        (project / "uv.lock").write_text(lock(boto3="1.43.75"))

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(with_options(baseline))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 1
        self._assert_withheld(project, baseline)

    def test_a_missing_version_parser_fails_before_uv_runs(self, monkeypatch, tmp_path):
        """`packaging` absent must read as `packaging` absent.

        Every comparison in this module degrades to "cannot compare" without it,
        and the rollback gate reports what it cannot compare — so a run in that
        state accuses every ordinary upgrade of moving backwards. Observed while
        probing the driver under an interpreter without it: six forward moves
        reported as regressions. Fail on the real cause, before uv spends a
        minute resolving, and hold the branch while doing it.
        """
        baseline = lock(boto3="1.43.72")
        project = self._repo(tmp_path, baseline)

        def fail_if_called(command, cwd):
            raise AssertionError("uv must not run when versions cannot be compared")

        monkeypatch.setattr(bounded, "Version", None)
        monkeypatch.setattr(bounded, "run_uv_lock", fail_if_called)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 1
        self._assert_withheld(project, baseline)

    def test_a_hold_is_an_ordinary_no_op_when_the_caller_owns_the_commit(
        self, monkeypatch, tmp_path, capsys
    ):
        """The lane distinction, asserted against the case above.

        application-sdk's own lane runs from a workflow that owns the push, so a
        bound admitting nothing means "commit the baseline and let the PR go
        net-empty". Failing there pushes nothing and leaves Renovate's unbounded
        commit standing on the branch — which is how a 4-hour-old boto3 reached
        main on 2026-08-20.
        """
        baseline = lock(boto3="1.43.74")
        project = self._repo(tmp_path, baseline)
        (project / "uv.lock").write_text(lock(boto3="1.43.75"))

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(with_options(baseline))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        exit_code = bounded.main(
            [
                "--window",
                "P3D",
                "--project-dir",
                str(project),
                "--caller-owns-commit",
            ]
        )
        assert exit_code == 0
        # The bounded resolve, stripped: versions match the baseline so the
        # caller commits a net-empty PR rather than a held branch.
        assert (project / "uv.lock").read_text() == baseline
        # The window's contents still belong in the log even though this lane
        # does not fail on them — otherwise a daily no-op PR is silent about
        # what Renovate wanted.
        assert "boto3 1.43.74 -> 1.43.75" in capsys.readouterr().err

    def test_caller_owns_commit_keeps_bounded_bytes_when_versions_match(
        self, monkeypatch, tmp_path
    ):
        """Versions matching the baseline is not the same as writing the baseline.

        The bounded resolve can carry extra metadata the committed lock does not.
        The caller-owns path writes that resolve (stripped of [options]), not the
        baseline text — writing the baseline would risk a lock that no longer
        satisfies the current pyproject.toml.
        """
        baseline = lock(boto3="1.43.74")
        project = self._repo(tmp_path, baseline)
        (project / "uv.lock").write_text(lock(boto3="1.43.75"))
        # Same versions as the baseline, plus a metadata line strip_options keeps.
        bounded_resolve = lock(boto3="1.43.74") + "# resolver-metadata: extra\n"

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(with_options(bounded_resolve))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        exit_code = bounded.main(
            [
                "--window",
                "P3D",
                "--project-dir",
                str(project),
                "--caller-owns-commit",
            ]
        )
        assert exit_code == 0
        final = (project / "uv.lock").read_text()
        assert bounded.lock_versions(final) == bounded.lock_versions(baseline)
        assert final != baseline
        assert "# resolver-metadata: extra" in final

    def test_a_rejected_downgrade_still_fails_when_the_caller_owns_the_commit(
        self, monkeypatch, tmp_path
    ):
        # The flag narrows the hold-everything case only. A genuine refusal still
        # refuses in either lane.
        baseline = lock(pytest_timeout="2.5.0")
        project = self._repo(tmp_path, baseline)

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(
                with_options(lock(pytest_timeout="2.4.0"))
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert (
            bounded.main(
                [
                    "--window",
                    "P3D",
                    "--project-dir",
                    str(project),
                    "--caller-owns-commit",
                ]
            )
            == 1
        )

    def test_a_genuinely_quiet_run_is_left_clean_and_passes(
        self, monkeypatch, tmp_path
    ):
        """The control for the test above.

        Renovate's own resolve moved nothing either ("uv.lock is unchanged"), so
        there is no unbounded copy to withhold and no reason to fail. The tree is
        left byte-identical to the baseline and the run succeeds — otherwise every
        quiet run in the fleet would red a required check.
        """
        baseline = lock(boto3="1.43.74")
        project = self._repo(tmp_path, baseline)

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(with_options(baseline))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 0
        assert (project / "uv.lock").read_text() == baseline

    def test_a_real_upgrade_still_strips_and_passes(self, monkeypatch, tmp_path):
        # The success path must be untouched: a bounded upgrade strips [options]
        # and exits 0, or nothing would ever merge again.
        baseline = lock(boto3="1.43.70")
        project = self._repo(tmp_path, baseline)
        (project / "uv.lock").write_text(lock(boto3="1.43.75"))
        resolved = lock(boto3="1.43.72")

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(with_options(resolved))
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 0
        assert (project / "uv.lock").read_text() == resolved

    def test_a_rejected_downgrade_is_withheld(self, monkeypatch, tmp_path):
        """The yanked-pin case: the base branch pins a release upstream has
        yanked, so every resolve comes back lower. Neither the downgrade nor
        Renovate's unbounded copy may reach the branch."""
        baseline = lock(pytest_timeout="2.5.0")
        project = self._repo(tmp_path, baseline)
        (project / "uv.lock").write_text(lock(pytest_timeout="2.4.0"))

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(
                with_options(lock(pytest_timeout="2.4.0"))
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 1
        self._assert_withheld(project, baseline)
        assert '"2.4.0"' not in (project / "uv.lock").read_text()

    def test_a_failed_resolve_is_withheld(self, monkeypatch, tmp_path):
        # uv can rewrite the lock and then fail; whatever it left behind is what
        # Renovate would commit.
        baseline = lock(boto3="1.43.72")
        project = self._repo(tmp_path, baseline)

        def fake_run(command, cwd):
            (Path(cwd) / "uv.lock").write_text(LOCK_WITH_OPTIONS)
            return subprocess.CompletedProcess(
                command, 1, "", "error: something else entirely"
            )

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 1
        self._assert_withheld(project, baseline)

    def test_a_failed_retry_is_withheld(self, monkeypatch, tmp_path):
        baseline = lock(cryptography="46.0.5")
        project = self._repo(
            tmp_path,
            baseline,
            "[project]\nname = 'app'\n\n[tool.uv]\n"
            'constraint-dependencies = ["cryptography>=46.0.5"]\n',
        )
        calls: list[list[str]] = []

        def fake_run(command, cwd):
            calls.append(command)
            (Path(cwd) / "uv.lock").write_text(LOCK_WITH_OPTIONS)
            return subprocess.CompletedProcess(
                command, 1, "", "error: No solution found ... cryptography>=46.0.5 ..."
            )

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 1
        assert len(calls) == 2, "the floor should have earned exactly one retry"
        self._assert_withheld(project, baseline)

    def test_a_new_lockfile_is_tripwired_too(self, monkeypatch, tmp_path):
        """The branch that ADDS uv.lock still has to be held.

        There is no committed baseline to write back here, and the pre-fix
        behaviour — leave the tree as it is — was the one refusal that landed on
        no required check: a valid-but-unbounded lock that Renovate commits and
        every gate waves through.
        """
        project = self._repo(tmp_path, lock(boto3="1.43.72"))
        subprocess.run(["git", "rm", "-q", "uv.lock"], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "drop the lock"],
            cwd=project,
            check=True,
            env=self.GIT_ENV,
        )
        assert bounded.baseline_lock_text(project) is None

        # What Renovate left: a valid, unbounded lock. uv then fails WITHOUT
        # rewriting it, which is what makes this the dangerous shape — the tree
        # holds something installable that every required check waves through.
        unbounded = lock(boto3="1.43.75")
        (project / "uv.lock").write_text(unbounded)

        def fake_run(command, cwd):
            return subprocess.CompletedProcess(
                command, 1, "", "error: something else entirely"
            )

        monkeypatch.setattr(bounded, "run_uv_lock", fake_run)
        assert bounded.main(["--window", "P3D", "--project-dir", str(project)]) == 1
        on_disk = (project / "uv.lock").read_text()
        assert "[options]" in on_disk, "a new lockfile must be held like any other"
        assert bounded.strip_options(on_disk) == unbounded

    def test_withhold_only_no_ops_on_an_empty_file(self, tmp_path):
        # Nothing committed AND nothing on disk: there is no content to tripwire,
        # and writing a bare [options] table would be a lock nobody can recover.
        target = tmp_path / "uv.lock"
        target.write_text("")
        assert (
            bounded.withhold(target, "", "P3D", reason=bounded.REFUSAL_WINDOW_EMPTY)
            is False
        )
        assert target.read_text() == ""

    def test_withhold_does_not_stack_options_tables(self, tmp_path):
        # The on-disk fallback is whatever the bounded resolve left, which already
        # carries uv's own [options]. Two tables would be invalid TOML rather than
        # a recoverable lock.
        target = tmp_path / "uv.lock"
        target.write_text(LOCK_WITH_OPTIONS)
        assert (
            bounded.withhold(target, "", "P3D", reason=bounded.REFUSAL_ROLLBACK) is True
        )
        written = target.read_text()
        assert written.count("[options]") == 1
        assert 'exclude-newer-span = "P3D"' in written
        assert bounded.strip_options(written) == bounded.strip_options(
            LOCK_WITH_OPTIONS
        )

    def test_the_tripwire_names_the_window_and_survives_a_strip(self, tmp_path):
        baseline = lock(boto3="1.43.72")
        target = tmp_path / "uv.lock"
        target.write_text(lock(boto3="1.43.99"))
        assert (
            bounded.withhold(
                target, baseline, "P3D", reason=bounded.REFUSAL_WINDOW_EMPTY
            )
            is True
        )
        written = target.read_text()
        assert 'exclude-newer-span = "P3D"' in written
        # Recoverable: a human, or the next run, gets the baseline back exactly.
        assert bounded.strip_options(written) == baseline

"""Tests for the self-healing lock-refusal reaper (FND-909)."""

from __future__ import annotations

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

import renovate_reap_refused_locks as reaper  # noqa: E402
import renovate_uv_lock_bounded as bounded  # noqa: E402

PACKAGES = """
[[package]]
name = "boto3"
version = "1.43.78"
source = { registry = "https://pypi.org/simple" }
"""


# A lock with no [options] table, shaped the way uv emits one. Kept free of
# stray blank runs because strip_options() collapses 3+ newlines, so a padded
# fixture would fail the round-trip assertion for a reason that is not a bug.
BASELINE = f"version = 1\nrevision = 3\n{PACKAGES}"


def lock_with(options: str) -> str:
    if not options:
        return BASELINE
    return f"version = 1\nrevision = 3\n\n{options}\n{PACKAGES}"


class TestRefusalReason:
    def test_reads_the_stamp_the_driver_writes(self):
        # Coupled to withhold() on purpose: this asserts the exact bytes the
        # driver produces, so a change to the stamp format fails here rather
        # than silently making every refusal unreapable.
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
        )
        assert reaper.refusal_reason(text) == bounded.REFUSAL_WINDOW_EMPTY

    @pytest.mark.parametrize(
        "reason",
        [
            bounded.REFUSAL_NO_PACKAGING,
            bounded.REFUSAL_UNSATISFIABLE_FLOOR,
            bounded.REFUSAL_FLOOR_ADMITTED_STILL_FAILED,
            bounded.REFUSAL_ROLLBACK,
        ],
    )
    def test_reads_every_standing_fault_reason(self, reason):
        text = lock_with(f'[options]\nexclude-newer-span = "P3D"  # refusal: {reason}')
        assert reaper.refusal_reason(text) == reason

    def test_no_options_table_is_none(self):
        assert reaper.refusal_reason(lock_with("")) is None

    def test_an_unstamped_tripwire_is_none(self):
        # The pre-FND-909 shape. Must NOT read as self-healing.
        text = lock_with('[options]\nexclude-newer-span = "P3D"')
        assert reaper.refusal_reason(text) is None

    def test_uvs_own_options_table_is_none(self):
        text = lock_with(
            '[options]\nexclude-newer = "0001-01-01T00:00:00Z"\n'
            'exclude-newer-span = "P7D"'
        )
        assert reaper.refusal_reason(text) is None

    def test_a_stamp_after_the_options_table_is_not_read(self):
        # A `# refusal:` string anywhere else in a 5000-line lock — a package
        # named for it, a URL fragment — must not be mistaken for the stamp.
        text = lock_with('[options]\nexclude-newer-span = "P3D"') + (
            '\n[[package]]\nname = "x"  # refusal: window-empty\n'
        )
        assert reaper.refusal_reason(text) is None

    def test_options_subtable_does_not_end_the_table(self):
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"\n'
            "[options.exclude-newer-package]\n"
            'aiohttp = "2026-08-06T00:00:00Z"'
        )
        assert reaper.refusal_reason(text) is None


class TestIsTripwire:
    """Found by running the census live: `[options] in text` called six ordinary
    refreshes refusals, because uv writes its own table in any repo that
    declares a bound in pyproject.toml."""

    def test_a_lone_span_is_the_drivers_tripwire(self):
        assert is_tripwire_lock('[options]\nexclude-newer-span = "P3D"') is True

    def test_a_stamped_span_is_the_drivers_tripwire(self):
        assert (
            is_tripwire_lock(
                '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
            )
            is True
        )

    def test_uvs_own_table_is_not_a_tripwire(self):
        # atlan-bw-app's shape: `[tool.uv] exclude-newer = "7 days"` in
        # pyproject makes uv record BOTH keys. withhold() only ever writes the
        # span, so the pair is proof uv wrote it.
        assert (
            is_tripwire_lock(
                '[options]\nexclude-newer = "0001-01-01T00:00:00Z"\n'
                'exclude-newer-span = "P7D"'
            )
            is False
        )

    def test_uvs_pinned_date_alone_is_not_a_tripwire(self):
        assert (
            is_tripwire_lock('[options]\nexclude-newer = "2026-08-18T10:29:39Z"')
            is False
        )

    def test_no_options_table_is_not_a_tripwire(self):
        assert reaper.is_tripwire(lock_with("")) is False

    def test_an_exclude_newer_package_subtable_does_not_confer_tripwire(self):
        assert (
            is_tripwire_lock(
                '[options]\nexclude-newer = "0001-01-01T00:00:00Z"\n'
                'exclude-newer-span = "P7D"\n'
                "[options.exclude-newer-package]\n"
                'aiohttp = "2026-08-06T00:00:00Z"'
            )
            is False
        )


def is_tripwire_lock(options: str) -> bool:
    return reaper.is_tripwire(lock_with(options))


class TestShouldReap:
    def stamped(self, reason: str) -> str:
        return lock_with(f'[options]\nexclude-newer-span = "P3D"  # refusal: {reason}')

    def test_reaps_the_self_healing_reason(self):
        assert should_reap_lock(self.stamped(bounded.REFUSAL_WINDOW_EMPTY)) is True

    @pytest.mark.parametrize(
        "reason",
        [
            bounded.REFUSAL_NO_PACKAGING,
            bounded.REFUSAL_UNSATISFIABLE_FLOOR,
            bounded.REFUSAL_FLOOR_ADMITTED_STILL_FAILED,
            bounded.REFUSAL_ROLLBACK,
        ],
    )
    def test_keeps_every_standing_fault(self, reason):
        # Reaping these would recycle a real wedge every four hours and hide it
        # behind a lane that looks busy.
        assert should_reap_lock(self.stamped(reason)) is False

    def test_keeps_an_unstamped_tripwire(self):
        assert should_reap_lock(lock_with('[options]\nexclude-newer-span = "P3D"')) is (
            False
        )

    def test_keeps_an_ordinary_lock_refresh(self):
        assert should_reap_lock(lock_with("")) is False

    def test_keeps_a_multi_file_pr_even_when_stamped(self):
        # withhold() writes the baseline back, so a genuine refusal can never
        # touch a second file. A stamped lock alongside another change is not a
        # refusal and must not be deleted.
        assert (
            reaper.should_reap(
                ["uv.lock", "pyproject.toml"],
                self.stamped(bounded.REFUSAL_WINDOW_EMPTY),
            )
            is False
        )

    def test_reaps_a_nested_lock(self):
        assert (
            reaper.should_reap(
                ["services/api/uv.lock"], self.stamped(bounded.REFUSAL_WINDOW_EMPTY)
            )
            is True
        )

    def test_keeps_a_lookalike_filename(self):
        assert (
            reaper.should_reap(
                ["my-uv.lock.bak"], self.stamped(bounded.REFUSAL_WINDOW_EMPTY)
            )
            is False
        )


def should_reap_lock(lock_text: str) -> bool:
    return reaper.should_reap(["uv.lock"], lock_text)


class TestFindRefusal:
    def fake_fetch(self, *, files, lock_text, pr_number=7):
        calls: list[str] = []

        def fetch(token, url, _method):
            calls.append(url)
            if "/pulls?" in url:
                return [{"number": pr_number, "head": {"sha": "abc1234def"}}]
            if url.endswith("/files?per_page=100"):
                return [{"filename": f} for f in files]
            if "/contents/" in url:
                return {"content": base64.b64encode(lock_text.encode()).decode()}
            raise AssertionError(f"unexpected url {url}")

        fetch.calls = calls  # type: ignore[attr-defined]
        return fetch

    def test_finds_a_self_healing_refusal(self):
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
        )
        fetch = self.fake_fetch(files=["uv.lock"], lock_text=text)
        pr = reaper.find_refusal("tok", "atlanhq/x", fetch)
        assert pr is not None and pr["number"] == 7

    def test_returns_none_for_a_standing_fault(self):
        text = lock_with('[options]\nexclude-newer-span = "P3D"  # refusal: rollback')
        fetch = self.fake_fetch(files=["uv.lock"], lock_text=text)
        assert reaper.find_refusal("tok", "atlanhq/x", fetch) is None

    def test_does_not_fetch_contents_for_a_multi_file_pr(self):
        # The short-circuit exists to keep the reaper to two API calls on the
        # common case; assert it rather than trusting it.
        fetch = self.fake_fetch(files=["uv.lock", "pyproject.toml"], lock_text="")
        assert reaper.find_refusal("tok", "atlanhq/x", fetch) is None
        assert not any("/contents/" in u for u in fetch.calls)

    def test_no_open_pr_is_none(self):
        def fetch(token, url, _method):
            return []

        assert reaper.find_refusal("tok", "atlanhq/x", fetch) is None

    def test_queries_only_the_lock_maintenance_branch(self):
        # The reaper deletes branches. It must never be able to select one
        # outside the preset's lock lane.
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
        )
        fetch = self.fake_fetch(files=["uv.lock"], lock_text=text)
        reaper.find_refusal("tok", "atlanhq/x", fetch)
        assert f"head=atlanhq:{reaper.BRANCH}" in fetch.calls[0]
        assert all(reaper.BRANCH in u or "/files?" in u for u in fetch.calls)


class TestIsDryRun:
    """A dry run that reaped for real would delete lock branches across the
    whole matrix and then skip opening the replacements — worse than the freeze
    this script clears. So the default direction is 'refuse to delete'."""

    def test_the_literal_null_the_workflow_sends_is_a_live_run(self):
        # `${{ inputs.dry_run || 'null' }}` on a scheduled pass.
        assert reaper.is_dry_run("null", False) is False

    @pytest.mark.parametrize("mode", ["full", "extract", "lookup"])
    def test_every_renovate_dry_run_mode_is_a_dry_run(self, mode):
        assert reaper.is_dry_run(mode, False) is True

    def test_an_unrecognised_mode_fails_safe(self):
        assert reaper.is_dry_run("something-new", False) is True

    def test_unset_is_a_live_run(self):
        # Direct invocation outside the workflow, where --dry-run is the lever.
        assert reaper.is_dry_run(None, False) is False
        assert reaper.is_dry_run("", False) is False

    def test_whitespace_around_null_is_still_live(self):
        assert reaper.is_dry_run("  null  ", False) is False

    def test_the_flag_wins_over_a_live_env(self):
        assert reaper.is_dry_run("null", True) is True


class TestMain:
    def test_a_dry_run_pass_deletes_nothing(self, monkeypatch, capsys):
        # The regression this guards: without the env check, `workflow_dispatch`
        # with dry_run=full deleted real branches across the matrix.
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "full")
        monkeypatch.setattr(
            reaper,
            "find_refusal",
            lambda *a, **k: {"number": 7, "head": {"sha": "a" * 8}},
        )
        deleted: list[str] = []
        monkeypatch.setattr(reaper, "_request", lambda *a, **k: deleted.append(a[1]))
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert deleted == []
        assert "dry run" in capsys.readouterr().out

    def test_a_live_pass_with_the_workflows_null_deletes(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "null")
        monkeypatch.setattr(
            reaper,
            "find_refusal",
            lambda *a, **k: {"number": 7, "head": {"sha": "a" * 8}},
        )
        deleted: list[str] = []
        monkeypatch.setattr(reaper, "_request", lambda *a, **k: deleted.append(a[1]))
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert deleted == [
            f"{reaper.API_ROOT}/repos/atlanhq/x/git/refs/heads/{reaper.BRANCH}"
        ]

    def test_repo_comes_from_target_repo_env(self, monkeypatch, capsys):
        # The workflow passes it as env so no matrix value lands in `run:`.
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("TARGET_REPO", "atlanhq/from-env")
        monkeypatch.setattr(reaper, "find_refusal", lambda *a, **k: None)
        assert reaper.main([]) == 0
        assert "atlanhq/from-env" in capsys.readouterr().out

    def test_no_repo_anywhere_fails(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.delenv("TARGET_REPO", raising=False)
        assert reaper.main([]) == 1

    def test_the_flag_alone_stops_a_delete_on_a_live_env(self, monkeypatch, capsys):
        # The env says live; the flag must still win, so a human can rehearse
        # against a real repo without deleting anything.
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "null")
        monkeypatch.setattr(
            reaper,
            "find_refusal",
            lambda *a, **k: {"number": 7, "head": {"sha": "a" * 8}},
        )
        deleted: list[str] = []
        monkeypatch.setattr(reaper, "_request", lambda *a, **k: deleted.append(a[1]))
        assert reaper.main(["--repo", "atlanhq/x", "--dry-run"]) == 0
        assert deleted == []
        assert "dry run" in capsys.readouterr().out

    def test_the_delete_is_a_DELETE_on_exactly_the_lock_branch_ref(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "null")
        monkeypatch.setattr(
            reaper,
            "find_refusal",
            lambda *a, **k: {"number": 7, "head": {"sha": "a" * 8}},
        )
        calls: list[tuple[str, str]] = []

        def fake_request(token, url, method="GET"):
            calls.append((url, method))

        monkeypatch.setattr(reaper, "_request", fake_request)
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert calls == [
            (
                f"{reaper.API_ROOT}/repos/atlanhq/x/git/refs/heads/{reaper.BRANCH}",
                "DELETE",
            )
        ]

    def test_missing_token_fails_loudly(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert reaper.main(["--repo", "atlanhq/x"]) == 1

    def test_an_api_failure_warns_and_does_not_stop_the_pass(self, monkeypatch, capsys):
        # Renovate runs after this step. A reaper outage must cost one cycle of
        # recovery latency, never the lock refresh itself.
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        def boom(*a, **k):
            raise TimeoutError("api down")

        monkeypatch.setattr(reaper, "find_refusal", boom)
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert "::warning::" in capsys.readouterr().out


class TestStampRoundTrip:
    """The driver and the reaper have to agree on the bytes, in both directions."""

    def test_withhold_writes_a_stamp_the_reaper_reads(self, tmp_path):
        target = tmp_path / "uv.lock"
        baseline = lock_with("")
        target.write_text(baseline)
        bounded.withhold(target, baseline, "P3D", reason=bounded.REFUSAL_WINDOW_EMPTY)
        written = target.read_text()
        assert reaper.refusal_reason(written) == bounded.REFUSAL_WINDOW_EMPTY
        assert reaper.should_reap(["uv.lock"], written) is True

    @pytest.mark.parametrize(
        "reason",
        [
            bounded.REFUSAL_NO_PACKAGING,
            bounded.REFUSAL_UNSATISFIABLE_FLOOR,
            bounded.REFUSAL_FLOOR_ADMITTED_STILL_FAILED,
            bounded.REFUSAL_ROLLBACK,
        ],
    )
    def test_a_standing_fault_round_trips_as_unreapable(self, tmp_path, reason):
        target = tmp_path / "uv.lock"
        baseline = lock_with("")
        target.write_text(baseline)
        bounded.withhold(target, baseline, "P3D", reason=reason)
        assert reaper.should_reap(["uv.lock"], target.read_text()) is False

    def test_the_stamp_never_survives_onto_a_green_lock(self, tmp_path):
        # strip_options() runs on the success path. If the stamp survived it, a
        # merged lock would carry a refusal marker and the reaper would delete
        # healthy branches.
        target = tmp_path / "uv.lock"
        baseline = lock_with("")
        target.write_text(baseline)
        bounded.withhold(target, baseline, "P3D", reason=bounded.REFUSAL_WINDOW_EMPTY)
        recovered = bounded.strip_options(target.read_text())
        assert recovered == baseline
        assert reaper.refusal_reason(recovered) is None

    def test_the_window_still_parses_with_a_stamp_present(self, tmp_path):
        # conformance.renovate.classify reads the window by splitting on '#'.
        # Assert the driver's own writer stays compatible with that reader.
        target = tmp_path / "uv.lock"
        baseline = lock_with("")
        target.write_text(baseline)
        bounded.withhold(target, baseline, "P3D", reason=bounded.REFUSAL_WINDOW_EMPTY)
        for line in target.read_text().splitlines():
            if line.strip().startswith("exclude-newer-span"):
                value = line.split("=", 1)[1].split("#")[0].strip().strip('"')
                assert value == "P3D"
                break
        else:
            raise AssertionError("no exclude-newer-span line was written")


def test_self_healing_set_is_exactly_the_window_case():
    # A guard on the blast radius: if someone adds a reason to
    # SELF_HEALING_REFUSALS, that is a decision to auto-delete branches carrying
    # it, and it should not pass review unnoticed.
    assert bounded.SELF_HEALING_REFUSALS == frozenset({bounded.REFUSAL_WINDOW_EMPTY})

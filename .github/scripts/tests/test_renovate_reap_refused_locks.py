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


class TestIsReapableLane:
    """Which branches the foreign-engine reap may touch at all. A hole here
    widens the set of branches the script can delete."""

    @pytest.mark.parametrize(
        "branch",
        [
            "renovate/lock-file-maintenance",
            "renovate/conformance-package",
            "renovate/app-contract-toolkit",
            "renovate/atlan-application-sdk-3.x-lockfile",
            "renovate/redis-8.x",
        ],
    )
    def test_managed_lanes_are_reapable(self, branch):
        assert reaper.is_reapable_lane(branch) is True

    @pytest.mark.parametrize(
        "branch", ["renovate/github-actions", "renovate/major-github-actions"]
    )
    def test_the_unmanaged_github_actions_lanes_are_not(self, branch):
        # self-hosted.js disables the github-actions manager, so nothing would
        # rebuild these. Deleting them destroys rather than recovers.
        assert reaper.is_reapable_lane(branch) is False

    @pytest.mark.parametrize(
        "branch", ["main", "feature/my-work", "renovate-but-not-really", ""]
    )
    def test_branches_outside_the_prefix_are_not(self, branch):
        assert reaper.is_reapable_lane(branch) is False

    def test_unmanaged_lanes_is_exactly_the_github_actions_pair(self):
        # A guard on the blast radius in the OTHER direction: removing an entry
        # here starts auto-deleting branches nothing recreates.
        assert reaper.UNMANAGED_LANES == frozenset(
            {"renovate/github-actions", "renovate/major-github-actions"}
        )


class TestHeadIsInRepo:
    """The ref deleted and the history inspected are two different things for a
    fork PR: we read head.ref but delete repos/<repo>/git/refs/heads/<head.ref>.
    Without this, a fork branch named renovate/<lane> whose commits GitHub
    attributes to a bot gets a ref deleted in the BASE repo, on the strength of
    a history that was never in it."""

    def pr(self, full_name):
        return {
            "head": {
                "ref": LOCK,
                "repo": {"full_name": full_name} if full_name else None,
            }
        }

    def test_a_branch_in_the_target_repo_qualifies(self):
        assert reaper.head_is_in_repo(self.pr("atlanhq/x"), "atlanhq/x") is True

    def test_a_fork_does_not(self):
        assert reaper.head_is_in_repo(self.pr("someone/x"), "atlanhq/x") is False

    def test_a_fork_inside_the_same_org_does_not(self):
        # `head=<owner>:<branch>` matches on the head OWNER, so this shape
        # satisfies the refusal path's own query while living elsewhere.
        assert reaper.head_is_in_repo(self.pr("atlanhq/x-fork"), "atlanhq/x") is False

    def test_a_deleted_fork_does_not(self):
        # head.repo goes null once the fork is gone. Fail closed, not match.
        assert reaper.head_is_in_repo(self.pr(None), "atlanhq/x") is False

    def test_a_payload_with_no_head_does_not(self):
        assert reaper.head_is_in_repo({}, "atlanhq/x") is False


class TestForeignOnlyHistory:
    """Every commit, not just the head — the guard that keeps a human's work."""

    def mend(self):
        return {"author": {"login": "renovate[bot]"}}

    def ours(self):
        return {"author": {"login": reaper.FLEET_ENGINE}}

    def human(self):
        return {"author": {"login": "some-engineer"}}

    def test_an_all_foreign_history_qualifies(self):
        assert reaper.foreign_only_history([self.mend(), self.mend()]) is True

    def test_a_human_commit_anywhere_disqualifies(self):
        # The case the head-only check got wrong: a human pushes a fix, a
        # foreign engine later pushes on top, and the head alone says "reap".
        assert reaper.foreign_only_history([self.human(), self.mend()]) is False

    def test_a_human_commit_at_the_head_disqualifies(self):
        assert reaper.foreign_only_history([self.mend(), self.human()]) is False

    def test_our_own_commit_anywhere_disqualifies(self):
        assert reaper.foreign_only_history([self.mend(), self.ours()]) is False

    def test_an_unresolved_author_disqualifies(self):
        assert reaper.foreign_only_history([self.mend(), {"author": None}]) is False

    def test_no_commits_is_false(self):
        # An API response that came back empty must not read as consent.
        assert reaper.foreign_only_history([]) is False

    def test_a_full_page_is_false_even_when_every_commit_is_foreign(self):
        # _request discards the Link header, so a full page and a truncated
        # history are indistinguishable. The commit that would be deleted
        # unseen is the one at position 101.
        page = [self.mend()] * reaper.COMMIT_PAGE_LIMIT
        assert reaper.foreign_only_history(page) is False

    def test_one_short_of_a_full_page_still_qualifies(self):
        # The boundary in the safe direction: a page that is provably complete.
        page = [self.mend()] * (reaper.COMMIT_PAGE_LIMIT - 1)
        assert reaper.foreign_only_history(page) is True


class TestIsForeignEngine:
    """An allowlist, not 'anyone who is not us' — the difference is whether a
    human who pushes a fix onto the lock branch keeps it."""

    def test_the_mend_hosted_app_is_foreign(self):
        assert reaper.is_foreign_engine("renovate[bot]") is True

    def test_our_own_runner_is_not_foreign(self):
        assert reaper.is_foreign_engine(reaper.FLEET_ENGINE) is False

    def test_a_human_is_not_foreign(self):
        assert reaper.is_foreign_engine("some-engineer") is False

    def test_an_unresolved_author_is_not_foreign(self):
        # GitHub returns author: null when it cannot map the commit to an
        # account. Unattributable must mean keep, not delete.
        assert reaper.is_foreign_engine(None) is False


LOCK = "renovate/lock-file-maintenance"


def commit_by(login):
    return {"author": {"login": login} if login is not None else None}


class TestFindReapable:
    def fake_fetch(
        self,
        *,
        files,
        lock_text,
        pr_number=7,
        branch=LOCK,
        authors=(reaper.FLEET_ENGINE,),
        head_repo="atlanhq/x",
    ):
        """A repo with exactly one open PR, on ``branch``, whose commits were
        written by ``authors`` in order.

        ``head_repo`` is the repository the head branch lives in — "atlanhq/x"
        (the target) for an ordinary PR, something else for a fork, or None for
        a fork that has since been deleted.
        """
        calls: list[str] = []
        pr = {
            "number": pr_number,
            "head": {
                "sha": "abc1234def",
                "ref": branch,
                "repo": {"full_name": head_repo} if head_repo else None,
            },
        }

        def fetch(token, url, _method):
            calls.append(url)
            if "/pulls?" in url:
                # find_refusal narrows by head=; find_foreign lists them all.
                if "head=" in url and f":{branch}" not in url:
                    return []
                return [pr]
            if url.endswith("/commits?per_page=100"):
                return [commit_by(a) for a in authors]
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
        found = reaper.find_reapable("tok", "atlanhq/x", fetch)
        assert len(found) == 1
        pr, reason = found[0]
        assert pr["number"] == 7
        assert "self-healing lock refusal" in reason

    def test_returns_nothing_for_a_standing_fault(self):
        text = lock_with('[options]\nexclude-newer-span = "P3D"  # refusal: rollback')
        fetch = self.fake_fetch(files=["uv.lock"], lock_text=text)
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_does_not_fetch_contents_for_a_multi_file_pr(self):
        fetch = self.fake_fetch(files=["uv.lock", "pyproject.toml"], lock_text="")
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []
        assert not any("/contents/" in u for u in fetch.calls)

    def test_no_open_pr_is_nothing(self):
        def fetch(token, url, _method):
            return []

        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_the_refusal_path_queries_only_the_lock_branch(self):
        # The refusal reap stays scoped to the lock lane: a refusal stamp is
        # written by the bounded-lock driver and exists nowhere else.
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
        )
        fetch = self.fake_fetch(files=["uv.lock"], lock_text=text)
        reaper.find_reapable("tok", "atlanhq/x", fetch)
        narrowed = [u for u in fetch.calls if "head=" in u]
        assert narrowed and all(f"head=atlanhq:{reaper.BRANCH}" in u for u in narrowed)

    def test_a_branch_is_never_reaped_twice(self):
        # find_foreign and find_refusal can both match the lock branch. Deleting
        # it once is recovery; queueing it twice is a second DELETE against a ref
        # that no longer exists.
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
        )
        fetch = self.fake_fetch(
            files=["uv.lock"], lock_text=text, authors=("renovate[bot]",)
        )
        found = reaper.find_reapable("tok", "atlanhq/x", fetch)
        assert len(found) == 1
        assert "every commit" in found[0][1]


class TestFindReapableForeignEngine:
    """The FND-1985 shape: a second engine pushes to the same branch name, its
    postUpgradeTasks are all rejected (allowedCommands is admin-only), and
    Renovate then wedges the branch permanently — it reads as human-edited
    (`unrecognizedAuthors`) AND its PR is invisible, because Renovate scopes its
    PR list to its own account."""

    def fetcher(self, **kwargs):
        kwargs.setdefault("files", ["uv.lock"])
        kwargs.setdefault("lock_text", lock_with(""))
        return TestFindReapable().fake_fetch(**kwargs)

    def test_reaps_a_branch_the_mend_app_wrote(self):
        # No refusal stamp anywhere: a hosted engine never runs the driver, so
        # the only evidence available is who wrote the commits.
        fetch = self.fetcher(authors=("renovate[bot]",))
        found = reaper.find_reapable("tok", "atlanhq/x", fetch)
        assert len(found) == 1
        pr, reason = found[0]
        assert pr["number"] == 7
        assert "renovate[bot]" in reason

    def test_the_same_branch_written_by_us_is_kept(self):
        # The red/green pair: identical but for the author, and this survives.
        fetch = self.fetcher(authors=(reaper.FLEET_ENGINE,))
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_a_human_push_onto_a_foreign_branch_keeps_the_branch(self):
        # The guard that a head-only check would get wrong.
        fetch = self.fetcher(authors=("some-engineer", "renovate[bot]"))
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_an_unresolved_author_is_kept(self):
        fetch = self.fetcher(authors=(None,))
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_reaps_a_foreign_conformance_package_branch(self):
        # atlan-netsuite-app#92, the case that proved the deadlock is not
        # lane-specific and that scoping the reaper to the lock branch left a
        # hole. No refusal path exists on this lane at all.
        fetch = self.fetcher(
            branch="renovate/conformance-package", authors=("renovate[bot]",)
        )
        found = reaper.find_reapable("tok", "atlanhq/x", fetch)
        assert len(found) == 1
        assert found[0][0]["head"]["ref"] == "renovate/conformance-package"

    @pytest.mark.parametrize(
        "branch", ["renovate/github-actions", "renovate/major-github-actions"]
    )
    def test_a_foreign_github_actions_branch_is_kept(self, branch):
        # The one lane where deleting destroys instead of recovering: the
        # manager is disabled, so nothing would rebuild it.
        fetch = self.fetcher(branch=branch, authors=("renovate[bot]",))
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_a_foreign_branch_is_reaped_without_reading_the_lock(self):
        # A hosted engine's lock has nothing in it for the refusal path to read.
        fetch = self.fetcher(authors=("renovate[bot]",))
        reaper.find_reapable("tok", "atlanhq/x", fetch)
        assert not any("/contents/" in u for u in fetch.calls)

    def test_a_fork_pr_is_never_reaped(self):
        # The ref we would DELETE is atlanhq/x's, while the commits inspected
        # live in the fork. Red/green pair with test_reaps_a_branch_the_mend_app
        # _wrote: identical but for where the head branch lives.
        fetch = self.fetcher(authors=("renovate[bot]",), head_repo="someone/x")
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_a_fork_pr_is_not_reaped_by_the_refusal_path_either(self):
        # `head=<owner>:<branch>` matches the head OWNER, so a same-org fork
        # satisfies that query while living in another repository.
        text = lock_with(
            '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
        )
        fetch = TestFindReapable().fake_fetch(
            files=["uv.lock"], lock_text=text, head_repo="atlanhq/x-fork"
        )
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_a_deleted_fork_is_never_reaped(self):
        fetch = self.fetcher(authors=("renovate[bot]",), head_repo=None)
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []

    def test_a_branch_whose_history_fills_a_page_is_kept_and_warned_about(self, capsys):
        # Cannot prove the history is foreign-only from one page, so keep it —
        # and say so, rather than letting it fall silently through the same door
        # as a branch proven to be ours.
        fetch = self.fetcher(authors=("renovate[bot]",) * reaper.COMMIT_PAGE_LIMIT)
        assert reaper.find_reapable("tok", "atlanhq/x", fetch) == []
        out = capsys.readouterr().out
        assert "::warning::" in out and "cannot prove" in out


def test_foreign_engine_set_is_exactly_the_mend_app():
    # A guard on the blast radius, matching the one on SELF_HEALING_REFUSALS:
    # adding an identity here is a decision to auto-delete branches it wrote.
    assert reaper.FOREIGN_ENGINES == frozenset({"renovate[bot]"})


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
            "find_reapable",
            lambda *a, **k: [
                ({"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}}, "a reason")
            ],
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
            "find_reapable",
            lambda *a, **k: [
                ({"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}}, "a reason")
            ],
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
        monkeypatch.setattr(reaper, "find_reapable", lambda *a, **k: [])
        assert reaper.main([]) == 0
        assert "atlanhq/from-env" in capsys.readouterr().out

    def test_several_branches_are_all_reaped_in_one_pass(self, monkeypatch):
        # A repo can have more than one lane wedged by the same foreign engine;
        # clearing only the first would leave the rest for another four hours.
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "null")
        monkeypatch.setattr(
            reaper,
            "find_reapable",
            lambda *a, **k: [
                ({"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}}, "r1"),
                (
                    {"number": 8, "head": {"sha": "b" * 8, "ref": "renovate/conf"}},
                    "r2",
                ),
            ],
        )
        deleted: list[str] = []
        monkeypatch.setattr(reaper, "_request", lambda *a, **k: deleted.append(a[1]))
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert deleted == [
            f"{reaper.API_ROOT}/repos/atlanhq/x/git/refs/heads/{LOCK}",
            f"{reaper.API_ROOT}/repos/atlanhq/x/git/refs/heads/renovate/conf",
        ]

    def test_one_branch_failing_to_delete_does_not_stop_the_others(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "null")
        monkeypatch.setattr(
            reaper,
            "find_reapable",
            lambda *a, **k: [
                ({"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}}, "r1"),
                (
                    {"number": 8, "head": {"sha": "b" * 8, "ref": "renovate/conf"}},
                    "r2",
                ),
            ],
        )
        deleted: list[str] = []

        def flaky(_token, url, method="GET"):
            if LOCK in url:
                raise TimeoutError("api down")
            deleted.append(url)

        monkeypatch.setattr(reaper, "_request", flaky)
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert deleted == [
            f"{reaper.API_ROOT}/repos/atlanhq/x/git/refs/heads/renovate/conf"
        ]
        assert "::warning::" in capsys.readouterr().out

    def test_the_reap_reason_reaches_the_log(self, monkeypatch, capsys):
        # The only record of WHICH shape fired. Without it a job log cannot
        # distinguish an expired refusal from a foreign engine overwriting us,
        # which is the difference between "working as designed" and "Mend is
        # still installed on this repo".
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("RENOVATE_DRY_RUN", "null")
        monkeypatch.setattr(
            reaper,
            "find_reapable",
            lambda *a, **k: [
                (
                    {"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}},
                    "every commit on it written by renovate[bot], "
                    "not atlan-app-fleet[bot]",
                )
            ],
        )
        monkeypatch.setattr(reaper, "_request", lambda *a, **k: None)
        assert reaper.main(["--repo", "atlanhq/x"]) == 0
        assert "written by renovate[bot]" in capsys.readouterr().out

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
            "find_reapable",
            lambda *a, **k: [
                ({"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}}, "a reason")
            ],
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
            "find_reapable",
            lambda *a, **k: [
                ({"number": 7, "head": {"sha": "a" * 8, "ref": LOCK}}, "a reason")
            ],
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

        monkeypatch.setattr(reaper, "find_reapable", boom)
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

"""Tests for .github/scripts/renovate_approval_conditions.py — the fleet's
code-owner approval boundary for Renovate PRs.

Every consumer repo calls the reusable workflow that drives this script, and it
decides what auto-merges unattended, so the bar here is not "the conditions are
implemented" but "no non-affirmative signal reaches the approval". The suite is
organised accordingly:

  * pure conditions, one class each, negative cases first;
  * :class:`TestFailClosed`, a sweep asserting that *nothing* but an affirmative
    signal approves;
  * :class:`TestExtractionParity`, the decision + log-line table characterised
    from the inline bash this replaced (FND-372), so the extraction stays
    provably behaviour-preserving rather than merely plausible;
  * :class:`TestDeliberateDivergences`, the two places the Python intentionally
    does NOT match that bash, each with the reason it is the safe direction.

The parity table was produced by running the workflow's original `run:` block
against a stubbed `gh` (real `jq`) over this exact scenario matrix and recording
the decision, exit status and stdout. 43 of 44 scenarios reproduce byte for
byte; the divergences are the two below.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import renovate_approval_conditions as gate  # noqa: E402

SHA = "abc123"
REPO = "owner/repo"


# ---------------------------------------------------------------------------
# Fixture builders — mirror the real GitHub payload shapes, because the script
# reads raw API JSON rather than a pre-shaped --jq projection.
# ---------------------------------------------------------------------------


def pr_payload(author="renovate[bot]", state="open", draft=False, head=SHA):
    return {
        "user": {"login": author},
        "state": state,
        "draft": draft,
        "head": {"sha": head},
    }


def file_payload(*names):
    return [{"filename": n} for n in names]


def status_payload(*pairs):
    return {"statuses": [{"context": c, "state": s} for c, s in pairs]}


GREEN_ARTIFACTS = status_payload((gate.ARTIFACT_CONTEXT, "success"))

APPROVED_REVIEW = {
    "user": {"login": "atlan-ci"},
    "state": "APPROVED",
    "body": f"{gate.APPROVAL_SIGNATURE} all required CI checks passed.\n",
}
DISMISSED_REVIEW = {**APPROVED_REVIEW, "state": "DISMISSED"}
HUMAN_REVIEW = {"user": {"login": "someone"}, "state": "APPROVED", "body": "lgtm"}
SDK_REVIEW_APPROVAL = {
    "user": {"login": "atlan-ci"},
    "state": "APPROVED",
    "body": "**SDK reviewer's verdict:** approved",
}


class FakeGh:
    """Stand-in for the `gh` CLI, serving canned payloads and recording calls.

    ``None`` for a payload simulates a non-2xx response: gh writes an error body
    to stdout and exits non-zero, which is the case the gate must never parse as
    a state. Listings served for ``--paginate --slurp`` are wrapped in an outer
    array, exactly as gh emits pages.
    """

    def __init__(
        self,
        *,
        meta=None,
        commit_pulls=None,
        files=None,
        status=None,
        reviews=None,
        checks_exit=0,
    ):
        self.meta = pr_payload() if meta is _UNSET else meta
        self.commit_pulls = (
            [{"state": "open", "number": 7}] if commit_pulls is _UNSET else commit_pulls
        )
        self.files = file_payload("uv.lock") if files is _UNSET else files
        self.status = GREEN_ARTIFACTS if status is _UNSET else status
        self.reviews = [] if reviews is _UNSET else reviews
        self.checks_exit = checks_exit
        self.calls: list[list[str]] = []
        self.approvals: list[list[str]] = []

    @staticmethod
    def _respond(payload, *, slurp=False):
        if payload is None:
            return json.dumps({"message": "Not Found"}), 1
        return json.dumps([payload] if slurp else payload), 0

    def __call__(self, cmd, check=False, capture_output=False, text=False):
        self.calls.append(cmd)
        args = cmd[1:]
        out, rc = "", 0
        if args[0] == "api":
            path = args[1]
            if path.endswith("/files"):
                out, rc = self._respond(self.files, slurp=True)
            elif path.endswith("/reviews"):
                out, rc = self._respond(self.reviews, slurp=True)
            elif path.endswith("/status"):
                out, rc = self._respond(self.status)
            elif path.endswith("/pulls"):
                out, rc = self._respond(self.commit_pulls)
            else:
                out, rc = self._respond(self.meta)
        elif args[:2] == ["pr", "checks"]:
            rc = self.checks_exit
        elif args[:2] == ["pr", "review"]:
            self.approvals.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    @property
    def api_paths(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "api"]


class _Unset:
    def __repr__(self):  # pragma: no cover - debugging aid
        return "<default>"


_UNSET = _Unset()


def _defaults(**over):
    base = dict(
        meta=_UNSET,
        commit_pulls=_UNSET,
        files=_UNSET,
        status=_UNSET,
        reviews=_UNSET,
        checks_exit=0,
    )
    base.update(over)
    return base


def run_main(monkeypatch, *, env=None, capsys=None, **gh_kwargs):
    """Drive ``main`` end to end against a :class:`FakeGh`."""
    environ = {
        "REPO": REPO,
        "EVENT_NAME": "workflow_run",
        "RUN_SHA": SHA,
        "DISPATCH_PR": "",
        "EXTRA_DEP_PATTERN": "",
    }
    environ.update(env or {})
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    fake = FakeGh(**_defaults(**gh_kwargs))
    code = gate.main(fake)
    log = capsys.readouterr().out if capsys else ""
    return code, fake, log


# ---------------------------------------------------------------------------
# Condition (a): author
# ---------------------------------------------------------------------------


class TestAuthor:
    @pytest.mark.parametrize(
        "author", ["a-human", "dependabot[bot]", "", "renovate", "atlan-app-fleet"]
    )
    def test_non_renovate_authors_are_refused(self, author):
        ok, msg = gate.check_author("7", author)
        assert not ok
        # The skip log is the only observability on why a PR was not approved,
        # so it must name the value that failed, not just the condition.
        assert f"author is '{author}'" in msg

    @pytest.mark.parametrize("author", ["atlan-app-fleet[bot]", "renovate[bot]"])
    def test_both_renovate_identities_are_accepted(self, author):
        assert gate.check_author("7", author)[0]

    def test_mend_identity_is_still_shipped(self):
        # application-sdk itself is still on Mend-hosted Renovate for its own
        # workflow-action updates. Dropping renovate[bot] silently stops
        # approving those; keep both until the SDK moves to the fleet runner.
        assert "renovate[bot]" in gate.RENOVATE_AUTHORS
        assert "atlan-app-fleet[bot]" in gate.RENOVATE_AUTHORS


# ---------------------------------------------------------------------------
# Condition (b): open and not draft
# ---------------------------------------------------------------------------


class TestOpenNotDraft:
    @pytest.mark.parametrize(
        "state,draft",
        [("closed", False), ("merged", False), ("", False), ("open", True)],
    )
    def test_anything_but_open_and_undrafted_is_refused(self, state, draft):
        assert not gate.check_open("7", state, draft)[0]

    def test_open_undrafted_passes(self):
        assert gate.check_open("7", "open", False)[0]

    def test_message_reports_the_jq_style_lowercase_bool(self):
        # Matches the log the inline bash emitted (jq renders booleans
        # lowercase), so operators reading old and new runs see one format.
        assert "draft='true'" in gate.check_open("7", "open", True)[1]
        assert "draft='false'" in gate.check_open("7", "closed", False)[1]


# ---------------------------------------------------------------------------
# Condition (c): race guard
# ---------------------------------------------------------------------------


class TestHeadUnchanged:
    def test_matching_sha_passes(self):
        assert gate.check_head_unchanged("7", SHA, SHA)[0]

    def test_moved_head_is_refused(self):
        ok, msg = gate.check_head_unchanged("7", "deadbeef", SHA)
        assert not ok
        assert SHA in msg and "deadbeef" in msg

    def test_empty_head_is_refused(self):
        # A metadata payload missing .head.sha must not compare equal to the
        # evaluated SHA by way of an empty default.
        assert not gate.check_head_unchanged("7", "", SHA)[0]


# ---------------------------------------------------------------------------
# Condition (d): the changed-file allowlist
# ---------------------------------------------------------------------------


class TestDepFileAllowlist:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/tests.yaml",
            ".github/dependabot.yml",
            "uv.lock",
            "packages/conformance/uv.lock",
            "package-lock.json",
            "requirements.txt",
            "pyproject.toml",
            "sub/dir/pyproject.toml",
            "contract/PklProject",
            "contract/PklProject.deps.json",
            "apps/foo/contract/PklProject",
            "app/generated/contract.pkl",
            "app/generated/nested/deep.pkl",
            "atlan.yaml",
            "app.yaml",
        ],
    )
    def test_dependency_paths_are_allowed(self, path):
        assert gate.non_dep_files([path]) == []

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "application_sdk/foo.py",
            "Dockerfile",
            # Lookalikes: the .github rule is a path prefix, not a substring.
            "x.github/foo.yml",
            "docs/.github/foo.yml",
            # The `.` in the manifest names is a literal, not "any character".
            "uvXlock",
            # ...and the (.*/)? prefix requires a directory boundary.
            "myuv.lock",
            "notrequirements.txt",
        ],
    )
    def test_non_dependency_paths_are_refused(self, path):
        assert gate.non_dep_files([path]) == [path]

    @pytest.mark.parametrize(
        "path", ["sub/atlan.yaml", "sub/app.yaml", "x/app/generated/a.pkl"]
    )
    def test_generated_artifacts_are_root_only(self, path):
        # renovate-pkl-sync only ever writes these at the repo root. An
        # unrelated app.yaml/atlan.yaml elsewhere in a consumer tree is ordinary
        # source and must not ride this gate.
        assert gate.non_dep_files([path]) == [path]

    def test_one_bad_file_among_many_good_ones_is_reported(self):
        files = ["uv.lock", "pyproject.toml", "application_sdk/foo.py"]
        assert gate.non_dep_files(files) == ["application_sdk/foo.py"]

    def test_all_offenders_are_reported_not_just_the_first(self):
        # The skip log lists them; truncating to one would hide what else the
        # PR touched.
        assert gate.non_dep_files(["a.py", "uv.lock", "b.py"]) == ["a.py", "b.py"]

    def test_extra_pattern_extends_the_allowlist(self):
        extra = r"packages/conformance/conformance/bootstrap/templates/.+\.ya?ml"
        path = "packages/conformance/conformance/bootstrap/templates/tests.yaml"
        assert gate.non_dep_files([path], extra) == []
        # ...and only when the caller actually declares it.
        assert gate.non_dep_files([path]) == [path]

    def test_extra_pattern_is_whole_line_matched(self):
        # Appended as another ERE alternation branch under full-match semantics
        # (the Python stand-in for `grep -vxE`), so a partial match must not
        # sneak a longer path through.
        assert gate.non_dep_files(["vendor/x.txt"], r"x\.txt") == ["vendor/x.txt"]
        assert gate.non_dep_files(["x.txt"], r"x\.txt") == []

    def test_empty_filenames_are_ignored(self):
        assert gate.non_dep_files(["", "uv.lock"]) == []


# ---------------------------------------------------------------------------
# Condition (f): renovate/artifacts classification
#
# This is the case the gate exists for. A failed postUpgradeTasks command does
# not stop Renovate — it commits, raises the PR and still enables platform
# automerge — so an artifacts status that is anything other than green must
# withhold approval, and a status that is ABSENT must too.
# ---------------------------------------------------------------------------


class TestArtifactState:
    def test_green_is_success(self):
        assert gate.classify_artifact_state(GREEN_ARTIFACTS) == "success"

    @pytest.mark.parametrize("state", ["failure", "pending", "error"])
    def test_other_states_are_passed_through_verbatim(self, state):
        # Reported verbatim so the skip log says which, not merely "not green".
        assert (
            gate.classify_artifact_state(status_payload((gate.ARTIFACT_CONTEXT, state)))
            == state
        )

    def test_absent_context_reads_as_missing(self):
        # The transition case: branches created before statusCheckWhen=always,
        # and any branch Renovate has not committed to. Must NOT read as green.
        assert (
            gate.classify_artifact_state(status_payload(("ci/build", "success")))
            == gate.ARTIFACT_MISSING
        )

    def test_empty_status_list_reads_as_missing(self):
        assert gate.classify_artifact_state(status_payload()) == gate.ARTIFACT_MISSING

    @pytest.mark.parametrize(
        "payload",
        [
            None,  # the fetch failed outright
            {},  # no statuses key
            {"statuses": None},
            {"statuses": "success"},  # wrong type
            {"message": "Not Found"},  # a non-2xx error body
            [],  # wrong top-level shape
            {"statuses": [{"context": gate.ARTIFACT_CONTEXT}]},  # no state key
            {"statuses": [{"context": gate.ARTIFACT_CONTEXT, "state": None}]},
        ],
    )
    def test_unreadable_payloads_read_as_missing(self, payload):
        assert gate.classify_artifact_state(payload) == gate.ARTIFACT_MISSING

    def test_first_matching_status_wins(self):
        # GitHub's combined-status endpoint lists the newest first; taking the
        # first preserves the original `| first` semantics.
        payload = status_payload(
            (gate.ARTIFACT_CONTEXT, "failure"), (gate.ARTIFACT_CONTEXT, "success")
        )
        assert gate.classify_artifact_state(payload) == "failure"

    def test_missing_sentinel_is_not_success(self):
        assert gate.ARTIFACT_MISSING != "success"

    def test_api_error_does_not_abort_the_step(self):
        # Unlike the metadata calls, an unreadable status classifies as missing
        # rather than raising — safe only because "missing" already blocks.
        fake = FakeGh(**_defaults(status=None))
        assert gate.fetch_artifact_state(REPO, SHA, fake) == gate.ARTIFACT_MISSING


# ---------------------------------------------------------------------------
# Condition (g): idempotency
# ---------------------------------------------------------------------------


class TestSignatureApprovals:
    def test_matching_approval_counts(self):
        assert gate.count_signature_approvals([APPROVED_REVIEW]) == 1

    def test_dismissed_approval_does_not_count(self):
        # dismiss_stale_reviews_on_push turns our review into DISMISSED, which
        # must let a fresh approval be posted for the new HEAD.
        assert gate.count_signature_approvals([DISMISSED_REVIEW]) == 0

    def test_human_approval_does_not_count(self):
        assert gate.count_signature_approvals([HUMAN_REVIEW]) == 0

    def test_sdk_review_approval_does_not_count(self):
        # @sdk-review posts as atlan-ci too but with its own signature. The two
        # automations are deliberately decoupled; neither may suppress the other.
        assert gate.count_signature_approvals([SDK_REVIEW_APPROVAL]) == 0

    @pytest.mark.parametrize(
        "review",
        [
            {"user": None, "state": "APPROVED", "body": gate.APPROVAL_SIGNATURE},
            {"user": {}, "state": "APPROVED", "body": gate.APPROVAL_SIGNATURE},
            {"user": {"login": "atlan-ci"}, "state": "APPROVED", "body": None},
            "not-a-dict",
        ],
    )
    def test_malformed_entries_do_not_count(self, review):
        assert gate.count_signature_approvals([review]) == 0

    def test_signature_is_the_documented_stable_string(self):
        # The dashboard scanner and this gate both key off it; changing it is a
        # cross-repo change, so pin the literal.
        assert gate.APPROVAL_SIGNATURE == "**Renovate auto-approval:**"

    def test_approval_body_starts_with_the_signature(self):
        assert gate.APPROVAL_BODY.startswith(gate.APPROVAL_SIGNATURE)
        assert (
            gate.count_signature_approvals(
                [
                    {
                        "user": {"login": "atlan-ci"},
                        "state": "APPROVED",
                        "body": gate.APPROVAL_BODY,
                    }
                ]
            )
            == 1
        )


# ---------------------------------------------------------------------------
# PR resolution
# ---------------------------------------------------------------------------


class TestResolvePrs:
    def test_workflow_run_selects_open_prs_at_the_run_sha(self):
        fake = FakeGh(
            **_defaults(
                commit_pulls=[
                    {"state": "open", "number": 7},
                    {"state": "closed", "number": 8},
                    {"state": "open", "number": 9},
                ]
            )
        )
        assert gate.resolve_prs(REPO, "workflow_run", SHA, "", fake) == (
            ["7", "9"],
            SHA,
        )

    def test_workflow_run_api_error_yields_no_candidates(self):
        # A failed commit→PRs lookup must not populate the candidate list with
        # tokens scraped from the error body — the bug the original bash's
        # exit-code gate was added to fix.
        fake = FakeGh(**_defaults(commit_pulls=None))
        assert gate.resolve_prs(REPO, "workflow_run", SHA, "", fake) == ([], SHA)

    def test_dispatch_evaluates_the_named_pr_at_its_current_head(self):
        fake = FakeGh(**_defaults(meta=pr_payload(head="feedface")))
        assert gate.resolve_prs(REPO, "workflow_dispatch", "", "7", fake) == (
            ["7"],
            "feedface",
        )

    def test_dispatch_api_error_aborts(self):
        fake = FakeGh(**_defaults(meta=None))
        with pytest.raises(gate.GhError):
            gate.resolve_prs(REPO, "workflow_dispatch", "", "7", fake)


# ---------------------------------------------------------------------------
# Fail-closed sweep
# ---------------------------------------------------------------------------


class TestFailClosed:
    """No non-affirmative signal may produce an approval."""

    @pytest.mark.parametrize(
        "name,kwargs",
        [
            ("author", dict(meta=pr_payload(author="a-human"))),
            ("closed", dict(meta=pr_payload(state="closed"))),
            ("draft", dict(meta=pr_payload(draft=True))),
            ("head moved", dict(meta=pr_payload(head="deadbeef"))),
            (
                "head missing",
                dict(meta={"user": {"login": "renovate[bot]"}, "state": "open"}),
            ),
            ("no files", dict(files=[])),
            ("source file", dict(files=file_payload("uv.lock", "src/a.py"))),
            ("checks red", dict(checks_exit=1)),
            ("checks pending", dict(checks_exit=8)),
            (
                "artifacts failure",
                dict(status=status_payload((gate.ARTIFACT_CONTEXT, "failure"))),
            ),
            (
                "artifacts pending",
                dict(status=status_payload((gate.ARTIFACT_CONTEXT, "pending"))),
            ),
            ("artifacts absent", dict(status=status_payload(("ci/build", "success")))),
            ("artifacts unreadable", dict(status=None)),
            ("already approved", dict(reviews=[APPROVED_REVIEW])),
        ],
    )
    def test_nothing_but_a_clean_pr_is_approved(
        self, monkeypatch, capsys, name, kwargs
    ):
        _code, fake, _log = run_main(monkeypatch, capsys=capsys, **kwargs)
        assert fake.approvals == [], f"{name} produced an approval"

    @pytest.mark.parametrize("field", ["meta", "files", "reviews"])
    def test_metadata_api_errors_abort_without_approving(
        self, monkeypatch, capsys, field
    ):
        # Inherited `set -euo pipefail` semantics: deciding on a partial view of
        # a PR is the one thing this gate must never do, so an unreadable
        # metadata/file/review listing fails the step outright.
        code, fake, log = run_main(monkeypatch, capsys=capsys, **{field: None})
        assert code == 1
        assert fake.approvals == []
        assert "::error::" in log

    def test_a_clean_pr_is_still_approved(self, monkeypatch, capsys):
        # The control: without it, every assertion above passes on a gate that
        # approves nothing at all.
        code, fake, log = run_main(monkeypatch, capsys=capsys)
        assert code == 0
        assert len(fake.approvals) == 1
        assert "✅ Approved PR #7" in log


# ---------------------------------------------------------------------------
# Orchestration: ordering, cost, isolation
# ---------------------------------------------------------------------------


class TestOrchestration:
    def test_each_open_pr_at_the_sha_is_evaluated(self, monkeypatch, capsys):
        # A commit can be the HEAD of several PRs; all of them get approved.
        _code, fake, log = run_main(
            monkeypatch,
            capsys=capsys,
            commit_pulls=[
                {"state": "open", "number": 7},
                {"state": "open", "number": 8},
            ],
        )
        assert [c[3] for c in fake.approvals] == ["7", "8"]
        assert "--- Evaluating PR #7 ---" in log and "--- Evaluating PR #8 ---" in log

    def test_one_pr_failing_a_condition_does_not_abort_the_rest(
        self, monkeypatch, capsys
    ):
        # Per-PR isolation. PR #7's HEAD has moved; #8 is clean and must still
        # be approved.
        calls: list[str] = []

        class Isolating(FakeGh):
            def __call__(self, cmd, **kw):
                calls.append(" ".join(cmd))
                if cmd[1] == "api" and cmd[2].endswith("/pulls/7"):
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps(pr_payload(head="moved")), stderr=""
                    )
                return super().__call__(cmd, **kw)

        for key, value in {
            "REPO": REPO,
            "EVENT_NAME": "workflow_run",
            "RUN_SHA": SHA,
            "DISPATCH_PR": "",
            "EXTRA_DEP_PATTERN": "",
        }.items():
            monkeypatch.setenv(key, value)
        fake = Isolating(
            **_defaults(
                commit_pulls=[
                    {"state": "open", "number": 7},
                    {"state": "open", "number": 8},
                ]
            )
        )
        code = gate.main(fake)
        log = capsys.readouterr().out
        assert code == 0
        assert "PR #7: HEAD moved" in log
        assert [c[3] for c in fake.approvals] == ["8"]

    def test_conditions_short_circuit_before_paying_for_later_calls(
        self, monkeypatch, capsys
    ):
        # A non-Renovate PR costs one API call, not six. This is also why the
        # evaluation order in the module docstring is load-bearing.
        _code, fake, _log = run_main(
            monkeypatch, capsys=capsys, meta=pr_payload(author="a-human")
        )
        assert not any(
            p.endswith(("/files", "/reviews", "/status")) for p in fake.api_paths
        )

    def test_artifact_status_is_read_before_the_review_is_posted(
        self, monkeypatch, capsys
    ):
        _code, fake, _log = run_main(monkeypatch, capsys=capsys)
        joined = [" ".join(c) for c in fake.calls]
        status_at = next(i for i, c in enumerate(joined) if c.endswith("/status"))
        review_at = next(i for i, c in enumerate(joined) if "pr review" in c)
        assert status_at < review_at

    def test_no_open_prs_exits_clean_without_touching_anything(
        self, monkeypatch, capsys
    ):
        code, fake, log = run_main(monkeypatch, capsys=capsys, commit_pulls=[])
        assert code == 0
        assert fake.approvals == []
        assert f"No open PRs found for SHA {SHA}" in log

    def test_approval_is_posted_as_a_review_with_the_exact_body(
        self, monkeypatch, capsys
    ):
        _code, fake, _log = run_main(monkeypatch, capsys=capsys)
        cmd = fake.approvals[0]
        assert cmd[:6] == ["gh", "pr", "review", "7", "--repo", REPO]
        assert "--approve" in cmd
        assert cmd[cmd.index("--body") + 1] == gate.APPROVAL_BODY

    def test_dispatch_path_evaluates_the_named_pr(self, monkeypatch, capsys):
        _code, fake, log = run_main(
            monkeypatch,
            capsys=capsys,
            env={"EVENT_NAME": "workflow_dispatch", "RUN_SHA": "", "DISPATCH_PR": "7"},
        )
        assert len(fake.approvals) == 1
        assert "--- Evaluating PR #7 ---" in log

    def test_required_checks_use_the_ruleset_not_a_hardcoded_list(
        self, monkeypatch, capsys
    ):
        # `gh pr checks --required` defers to the repo's own ruleset, so a repo
        # changing its required contexts needs no change here. A hardcoded list
        # would drift silently.
        _code, fake, _log = run_main(monkeypatch, capsys=capsys)
        checks = next(c for c in fake.calls if c[1:3] == ["pr", "checks"])
        assert "--required" in checks

    def test_paginated_listings_are_slurped_and_flattened(self, monkeypatch, capsys):
        # `gh api --paginate` alone emits one document per page. Every paginated
        # call must ask for --slurp so the pages arrive as one array.
        _code, fake, _log = run_main(monkeypatch, capsys=capsys)
        for cmd in fake.calls:
            if cmd[1] == "api" and "--paginate" in cmd:
                assert "--slurp" in cmd, cmd

    def test_multi_page_review_listings_are_flattened(self):
        assert gate._flatten_pages([[HUMAN_REVIEW], [APPROVED_REVIEW]]) == [
            HUMAN_REVIEW,
            APPROVED_REVIEW,
        ]


# ---------------------------------------------------------------------------
# Extraction parity (FND-372)
# ---------------------------------------------------------------------------


class TestExtractionParity:
    """Decision + log table characterised from the inline bash this replaced.

    Each case was executed against the workflow's original `run:` block with a
    stubbed `gh` before any code moved, and the expected log lines below are the
    lines that block actually printed. They are asserted verbatim because the
    skip log is the only observability on why a Renovate PR was not approved —
    a refactor that keeps the decision but loses the reason is still a
    regression.
    """

    CASES = [
        (
            "clean renovate PR",
            {},
            True,
            ["✅ Approved PR #7 as atlan-ci (Renovate auto-approval)."],
        ),
        (
            "fleet bot author",
            dict(meta=pr_payload(author="atlan-app-fleet[bot]")),
            True,
            ["✅ Approved PR #7 as atlan-ci (Renovate auto-approval)."],
        ),
        (
            "human author",
            dict(meta=pr_payload(author="a-human")),
            False,
            ["PR #7: author is 'a-human', not a Renovate bot — skipping."],
        ),
        (
            "closed PR",
            dict(meta=pr_payload(state="closed")),
            False,
            ["PR #7: state='closed' draft='false' — skipping."],
        ),
        (
            "draft PR",
            dict(meta=pr_payload(draft=True)),
            False,
            ["PR #7: state='open' draft='true' — skipping."],
        ),
        (
            "HEAD moved",
            dict(meta=pr_payload(head="deadbeef")),
            False,
            [
                f"PR #7: HEAD moved ({SHA} → deadbeef). "
                "Skipping — a later run will re-evaluate."
            ],
        ),
        (
            "no changed files",
            dict(files=[]),
            False,
            ["PR #7: no changed files found — skipping."],
        ),
        (
            "source file present",
            dict(files=file_payload("uv.lock", "application_sdk/foo.py")),
            False,
            [
                "PR #7: contains non-dependency files:",
                "  application_sdk/foo.py",
                "Skipping.",
            ],
        ),
        (
            "required checks red",
            dict(checks_exit=1),
            False,
            [
                "PR #7: checking required CI status...",
                "PR #7: required checks not yet all green — skipping.",
            ],
        ),
        (
            "artifacts failed",
            dict(status=status_payload((gate.ARTIFACT_CONTEXT, "failure"))),
            False,
            ["PR #7: renovate/artifacts is 'failure', not 'success' — skipping."],
        ),
        (
            "artifacts absent",
            dict(status=status_payload(("ci/build", "success"))),
            False,
            ["PR #7: renovate/artifacts is 'missing', not 'success' — skipping."],
        ),
        (
            "artifacts unreadable",
            dict(status=None),
            False,
            ["PR #7: renovate/artifacts is 'missing', not 'success' — skipping."],
        ),
        (
            "already approved",
            dict(reviews=[APPROVED_REVIEW]),
            False,
            [
                "PR #7: atlan-ci has already approved with the Renovate "
                "signature — skipping."
            ],
        ),
        (
            "dismissed approval is re-posted",
            dict(reviews=[DISMISSED_REVIEW]),
            True,
            ["✅ Approved PR #7 as atlan-ci (Renovate auto-approval)."],
        ),
        (
            "human approval does not suppress ours",
            dict(reviews=[HUMAN_REVIEW]),
            True,
            ["✅ Approved PR #7 as atlan-ci (Renovate auto-approval)."],
        ),
        (
            "no open PRs at the SHA",
            dict(commit_pulls=[]),
            False,
            [f"No open PRs found for SHA {SHA} — nothing to do."],
        ),
        (
            "commit→PRs lookup failed",
            dict(commit_pulls=None),
            False,
            [f"No open PRs found for SHA {SHA} — nothing to do."],
        ),
        (
            "only closed PRs at the SHA",
            dict(commit_pulls=[{"state": "closed", "number": 7}]),
            False,
            [f"No open PRs found for SHA {SHA} — nothing to do."],
        ),
    ]

    @pytest.mark.parametrize(
        "name,kwargs,approves,expected_lines",
        CASES,
        ids=[c[0] for c in CASES],
    )
    def test_matches_the_characterised_bash(
        self, monkeypatch, capsys, name, kwargs, approves, expected_lines
    ):
        _code, fake, log = run_main(monkeypatch, capsys=capsys, **kwargs)
        assert bool(fake.approvals) is approves, name
        for line in expected_lines:
            assert line in log, f"{name}: missing log line {line!r}"

    def test_approval_body_is_byte_identical_to_the_bash_heredoc(self):
        # Consumers see this text on every Renovate PR, and its first line is the
        # idempotency signature — so it is pinned in full, not by prefix.
        assert gate.APPROVAL_BODY == (
            "**Renovate auto-approval:** all required CI checks passed.\n"
            "\n"
            "This is an automated code-owner approval posted by `atlan-ci` for a\n"
            "dependency-only Renovate PR. It is automatically dismissed on any new\n"
            "push (`dismiss_stale_reviews_on_push`) and re-posted once the new\n"
            "HEAD's required checks are green."
        )


# ---------------------------------------------------------------------------
# Deliberate divergences from the extracted bash
# ---------------------------------------------------------------------------


class TestDeliberateDivergences:
    """The two places this script does NOT reproduce the bash it replaced.

    Both move in the fail-closed direction: the bash approved something it
    should not have, and this refuses. Recorded here so a future reader can tell
    an intentional correction from an extraction slip.
    """

    def test_invalid_extra_pattern_fails_the_step_instead_of_approving_everything(
        self, monkeypatch, capsys
    ):
        # The bash built the allowlist by string-appending extra_dep_file_pattern
        # into a `grep -vxE` whose non-zero exit was swallowed by `|| true`. An
        # unparseable pattern therefore made grep exit 2, the non-dep list come
        # back empty, and EVERY changed file — source included — read as
        # dependency-related. Characterising the original confirmed it: a PR
        # touching src/evil.py was approved. Refusing is the safe direction.
        code, fake, log = run_main(
            monkeypatch,
            capsys=capsys,
            env={"EXTRA_DEP_PATTERN": "["},
            files=file_payload("src/evil.py"),
        )
        assert code == 1
        assert fake.approvals == []
        assert "extra_dep_file_pattern is not a valid regular expression" in log

    def test_a_second_page_of_reviews_does_not_block_approval(self):
        # The bash counted approvals with `gh api --paginate --jq '... | length'`,
        # and --jq is applied PER PAGE — so a PR with two pages of reviews and no
        # prior approval produced "0\n0", which its `!= "0"` test read as
        # "already approved" and never approved again. --slurp collapses the
        # pages, so the count is one number.
        pages = [[HUMAN_REVIEW] * 30, [HUMAN_REVIEW]]
        assert gate.count_signature_approvals(gate._flatten_pages(pages)) == 0

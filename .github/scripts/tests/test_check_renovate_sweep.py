"""Tests for .github/scripts/check_renovate_sweep.py.

The fixtures below are excerpts from the real sweep that motivated the guard
(application-sdk run 35269258699, 2026-09-17), timestamp prefixes included —
the guard reads GHA log text, not a pristine Renovate stream, so a pattern that
only matches at column 0 would pass every test written from memory and fail on
the only input that matters.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_renovate_sweep as guard

# Verbatim from the sweep, one PR's worth.
AUTOMERGE_FAIL_BLOCK = (
    "2026-09-17T20:22:44.1593582Z DEBUG: PR created "
    "(repository=atlanhq/atlan-cassandra-dse-app, branch=renovate/conformance-package)\n"
    '2026-09-17T20:22:44.1594023Z        "pr": 153,\n'
    "2026-09-17T20:22:46.5911099Z DEBUG: GitHub-native automerge: fail "
    "(repository=atlanhq/atlan-cassandra-dse-app, branch=renovate/conformance-package)\n"
    '2026-09-17T20:22:46.5911544Z        "prNumber": 153,\n'
    '2026-09-17T20:22:46.5911747Z        "errors": [\n'
    "2026-09-17T20:22:46.5911921Z          {\n"
    '2026-09-17T20:22:46.5912529Z            "type": "RATE_LIMIT",\n'
    '2026-09-17T20:22:46.5912757Z            "code": "graphql_rate_limit",\n'
    '2026-09-17T20:22:46.5913080Z            "message": "API rate limit already '
    'exceeded for installation ID 145689782."\n'
    "2026-09-17T20:22:46.5913429Z          }\n"
    "2026-09-17T20:22:46.5913673Z        ]\n"
    "2026-09-17T20:22:46.5914178Z  INFO: PR created\n"
)

AUTOMERGE_SUCCESS_LINE = (
    "2026-09-17T20:07:53.8953407Z DEBUG: GitHub-native automerge: success...PrNo: 232 "
    "(repository=atlanhq/atlan-clickhouse-app, branch=renovate/atlan-application-sdk-3.x-lockfile)\n"
)

INIT_AUTH_FAILURE = (
    "2026-09-17T20:24:35.1810031Z DEBUG: Error authenticating with GitHub\n"
    '2026-09-17T20:24:35.1811021Z        "err": {"message": "Init: Can\'t get App details"}\n'
    "2026-09-17T20:24:35.1825649Z FATAL: Initialization error\n"
)

CLEAN_SWEEP = (
    AUTOMERGE_SUCCESS_LINE
    + "2026-09-17T20:08:06.9618224Z DEBUG: Branch does not need updating\n"
    + "2026-09-17T20:08:07.0000000Z  INFO: Repository finished\n"
)


@dataclass(frozen=True)
class ReportCase:
    """One (log, expected exit code) pair for the top-level report()."""

    name: str
    log: str
    expected_code: int


REPORT_CASES: tuple[ReportCase, ...] = (
    ReportCase("clean sweep", CLEAN_SWEEP, 0),
    ReportCase("automerge rate-limited", AUTOMERGE_FAIL_BLOCK, 1),
    ReportCase("auth init exhausted", INIT_AUTH_FAILURE, 1),
)


class TestFindAutomergeFailures:
    def test_extracts_repository_branch_pr_and_code(self):
        failures = guard.find_automerge_failures(AUTOMERGE_FAIL_BLOCK)
        assert len(failures) == 1
        assert failures[0] == guard.AutomergeFailure(
            repository="atlanhq/atlan-cassandra-dse-app",
            branch="renovate/conformance-package",
            pr_number=153,
            error_code="graphql_rate_limit",
        )

    def test_ignores_the_success_line(self):
        """`success...PrNo:` shares the prefix `GitHub-native automerge: `.

        The guard must key on `fail`, not on the phrase — matching the phrase
        would red every healthy sweep, which is worse than the bug.
        """
        assert guard.find_automerge_failures(AUTOMERGE_SUCCESS_LINE) == []

    def test_finds_every_failure_in_a_multi_pr_sweep(self):
        failures = guard.find_automerge_failures(AUTOMERGE_FAIL_BLOCK * 3)
        assert [f.pr_number for f in failures] == [153, 153, 153]

    def test_survives_a_header_with_no_detail_block(self):
        """Truncated logs must degrade, not crash: the header alone still counts."""
        header_only = AUTOMERGE_FAIL_BLOCK.splitlines()[2] + "\n"
        failures = guard.find_automerge_failures(header_only)
        assert len(failures) == 1
        assert failures[0].pr_number is None
        assert failures[0].error_code is None

    def test_does_not_attribute_a_distant_pr_number_to_the_header(self):
        """A detail block further away than the window is a different event."""
        header = AUTOMERGE_FAIL_BLOCK.splitlines()[2] + "\n"
        far = (
            header + ("filler\n" * (guard._DETAIL_WINDOW + 2)) + '  "prNumber": 999,\n'
        )
        assert guard.find_automerge_failures(far)[0].pr_number is None


class TestFindBudgetHits:
    def test_flags_the_graphql_rate_limit_code(self):
        markers = [hit.marker for hit in guard.find_budget_hits(AUTOMERGE_FAIL_BLOCK)]
        assert "graphql_rate_limit" in markers
        assert "API rate limit already exceeded" in markers

    def test_flags_the_init_auth_failure(self):
        markers = [hit.marker for hit in guard.find_budget_hits(INIT_AUTH_FAILURE)]
        assert markers == ["Init: Can't get App details"]

    def test_deduplicates_by_marker(self):
        """One exhausted budget emits hundreds of these; report each wall once."""
        hits = guard.find_budget_hits(INIT_AUTH_FAILURE * 50)
        assert len(hits) == 1

    def test_clean_sweep_has_no_hits(self):
        assert guard.find_budget_hits(CLEAN_SWEEP) == []


class TestReport:
    @pytest.mark.parametrize("case", REPORT_CASES, ids=lambda c: c.name)
    def test_exit_code(self, case: ReportCase):
        code, _ = guard.report(case.log, "atlanhq/atlan-cassandra-dse-app")
        assert code == case.expected_code

    def test_failure_message_names_the_pr_that_cannot_merge(self):
        _, message = guard.report(
            AUTOMERGE_FAIL_BLOCK, "atlanhq/atlan-cassandra-dse-app"
        )
        assert "atlanhq/atlan-cassandra-dse-app#153" in message
        assert "graphql_rate_limit" in message

    def test_clean_message_names_the_repo(self):
        _, message = guard.report(CLEAN_SWEEP, "atlanhq/atlan-clickhouse-app")
        assert "atlanhq/atlan-clickhouse-app" in message


class TestMain:
    def test_missing_log_fails_open(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """A vanished log must not red a sweep that Renovate itself passed."""
        sys.argv = [
            "check_renovate_sweep.py",
            "--log",
            str(tmp_path / "absent.log"),
            "--repo",
            "atlanhq/atlan-clickhouse-app",
        ]
        assert guard.main() == 0
        assert "::warning::" in capsys.readouterr().out

    def test_real_failure_log_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        log = tmp_path / "renovate.log"
        log.write_text(AUTOMERGE_FAIL_BLOCK)
        sys.argv = [
            "check_renovate_sweep.py",
            "--log",
            str(log),
            "--repo",
            "atlanhq/atlan-cassandra-dse-app",
        ]
        assert guard.main() == 1
        assert "::error::" in capsys.readouterr().out

    def test_clean_log_exits_zero(self, tmp_path: Path):
        log = tmp_path / "renovate.log"
        log.write_text(CLEAN_SWEEP)
        sys.argv = [
            "check_renovate_sweep.py",
            "--log",
            str(log),
            "--repo",
            "atlanhq/atlan-clickhouse-app",
        ]
        assert guard.main() == 0

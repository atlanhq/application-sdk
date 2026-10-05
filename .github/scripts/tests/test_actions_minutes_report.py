"""Tests for .github/scripts/actions_minutes_report.py.

`gh` is stubbed through the `run` seam, so no real API calls are made. The
stub answers the three call shapes the script makes — the runs listing, the
GraphQL check-suite batch and the REST jobs fallback — from in-memory fixtures.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions_minutes_report as amr

UTC = timezone.utc


def _run(
    run_id,
    created,
    *,
    name="Tests",
    event="pull_request",
    branch="feat/x",
    sha="a1",
    actor="dev",
):
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "head_branch": branch,
        "head_sha": sha,
        "created_at": created,
        "run_attempt": 1,
        "triggering_actor": {"login": actor},
        "check_suite_node_id": f"CS_{run_id}",
    }


def _check(name, start, end, conclusion="SUCCESS"):
    return {
        "name": name,
        "startedAt": start,
        "completedAt": end,
        "conclusion": conclusion,
    }


class FakeGh:
    """Stub `gh`: runs keyed by repo, check runs keyed by suite id."""

    def __init__(
        self, runs, checks, *, visibility="private", rest_jobs=None, gateway_over=None
    ):
        self.runs = runs
        self.checks = checks
        self.visibility = visibility
        self.rest_jobs = rest_jobs or {}
        self.gateway_over = gateway_over
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ["api", "graphql"]:
            ids = json.loads(stdin)["variables"]["ids"]
            if self.gateway_over is not None and len(ids) > self.gateway_over:
                return 1, "", "gh: HTTP 502: Bad Gateway"
            nodes = []
            for i in ids:
                checks = self.checks.get(i, [])
                nodes.append(
                    {
                        "id": i,
                        "checkRuns": {
                            "totalCount": len(checks),
                            "nodes": checks[: amr.CHECK_RUNS_PAGE],
                        },
                    }
                )
            return 0, json.dumps({"data": {"nodes": nodes}}), ""
        if args[0] == "api" and args[1].startswith("repos/") and len(args) == 4:
            return 0, json.dumps({"visibility": self.visibility}), ""
        path = args[3]
        params = dict(a.split("=", 1) for a in args[5::2])
        if path.endswith("/jobs"):
            run_id = int(path.split("/")[-2])
            jobs = self.rest_jobs[run_id]
            page = int(params["page"])
            chunk = jobs[(page - 1) * 100 : page * 100]
            return 0, json.dumps({"total_count": len(jobs), "jobs": chunk}), ""
        repo = path.removeprefix("repos/").removesuffix("/actions/runs")
        lo, hi = (amr.parse_ts(t) for t in params["created"].split(".."))
        matching = [
            r
            for r in self.runs.get(repo, [])
            if lo <= amr.parse_ts(r["created_at"]) <= hi
        ]
        page = int(params["page"])
        per = int(params["per_page"])
        visible = matching[: amr.RUNS_RESULT_CAP]
        chunk = visible[(page - 1) * per : page * per]
        return 0, json.dumps({"total_count": len(matching), "workflow_runs": chunk}), ""


def test_run_seam_defaults_to_real_gh_wrapper():
    assert inspect.signature(amr.main).parameters["run"].default is amr._run_gh


# ── classification and rounding ────────────────────────────────────────────


@pytest.mark.parametrize(
    "branch,actor,lane",
    [
        ("renovate/lock-file-maintenance", "atlan-app-fleet[bot]", amr.Lane.RENOVATE),
        ("bump-version-main", "atlan-app-fleet[bot]", amr.Lane.BUMP_VERSION),
        ("main", "github-merge-queue[bot]", amr.Lane.OTHER_BOT),
        ("feat/x", "some-dev", amr.Lane.HUMAN),
        ("", "some-dev", amr.Lane.HUMAN),
    ],
)
def test_lane_reads_branch_before_login(branch, actor, lane):
    # The fleet App opens both Renovate and version-bump PRs, so the login
    # alone cannot tell them apart.
    assert amr.classify_lane(branch, actor) is lane


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:04Z", (4, 1)),
        ("2026-09-30T10:00:00Z", "2026-09-30T10:01:00Z", (60, 1)),
        ("2026-09-30T10:00:00Z", "2026-09-30T10:01:01Z", (61, 2)),
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:00Z", (0, 0)),
        (None, "2026-09-30T10:00:00Z", (0, 0)),
        ("2026-09-30T10:00:00Z", None, (0, 0)),
    ],
)
def test_job_minutes_rounds_each_job_up(start, end, expected):
    assert amr.job_minutes(start, end) == expected


def test_day_windows_are_inclusive_utc_days():
    windows = amr.day_windows(date(2026, 9, 28), date(2026, 9, 29))
    assert [(amr.fmt_ts(a), amr.fmt_ts(b)) for a, b in windows] == [
        ("2026-09-28T00:00:00Z", "2026-09-28T23:59:59Z"),
        ("2026-09-29T00:00:00Z", "2026-09-29T23:59:59Z"),
    ]


def test_day_windows_rejects_inverted_range():
    with pytest.raises(ValueError):
        amr.day_windows(date(2026, 9, 29), date(2026, 9, 28))


def test_select_repos_sample_is_seeded_and_sorted():
    fleet = [f"atlanhq/atlan-{i}-app" for i in range(20)]
    a = amr.select_repos(fleet, [], 5, seed=7)
    assert a == amr.select_repos(list(reversed(fleet)), [], 5, seed=7)
    assert a == sorted(a) and len(a) == 5
    assert amr.select_repos(fleet, [], 50, seed=7) == sorted(fleet)
    assert amr.select_repos(fleet, ["atlanhq/z", "atlanhq/a"], 5, 7) == [
        "atlanhq/a",
        "atlanhq/z",
    ]


# ── fetching ────────────────────────────────────────────────────────────────


def test_window_over_the_cap_is_split_until_every_run_is_seen(monkeypatch):
    # 30 runs in one day against a cap of 10: a single query would page out at
    # 10 and silently drop 20. Splitting must recover all 30.
    monkeypatch.setattr(amr, "RUNS_RESULT_CAP", 10)
    monkeypatch.setattr(amr, "RUNS_PAGE_SIZE", 4)
    base = datetime(2026, 9, 30, tzinfo=UTC)
    runs = [_run(i, amr.fmt_ts(base + timedelta(minutes=40 * i))) for i in range(30)]
    gh = FakeGh({"o/r": runs}, {})
    got = amr.list_runs("o/r", date(2026, 9, 30), date(2026, 9, 30), gh)
    assert sorted(r.run_id for r in got) == list(range(30))


def test_unsplittable_window_over_the_cap_fails_loudly(monkeypatch):
    monkeypatch.setattr(amr, "RUNS_RESULT_CAP", 2)
    runs = [_run(i, "2026-09-30T12:00:00Z") for i in range(3)]
    with pytest.raises(amr.ReportError, match="cannot be split"):
        amr.list_runs(
            "o/r", date(2026, 9, 30), date(2026, 9, 30), FakeGh({"o/r": runs}, {})
        )


def test_run_record_fields():
    raw = _run(
        5, "2026-10-01T09:00:00Z", branch="renovate/x", actor="atlan-app-fleet[bot]"
    )
    rec = amr.to_run_record("o/r", "private", raw)
    assert rec.lane == "renovate"
    assert rec.weekday == "Thu"
    assert rec.check_suite_node_id == "CS_5"


def test_jobs_come_from_batched_graphql(monkeypatch):
    monkeypatch.setattr(amr, "SUITE_BATCH", 2)
    runs = [
        amr.to_run_record("o/r", "private", _run(i, "2026-09-30T10:00:00Z"))
        for i in range(5)
    ]
    checks = {
        f"CS_{i}": [_check("unit", "2026-09-30T10:00:00Z", "2026-09-30T10:00:30Z")]
        for i in range(5)
    }
    gh = FakeGh({}, checks)
    jobs = amr.fetch_jobs(runs, gh)
    assert len(jobs) == 5
    assert all(j.billed_minutes == 1 and j.wall_seconds == 30 for j in jobs)
    graphql_calls = [c for c in gh.calls if c[0][:2] == ["api", "graphql"]]
    assert len(graphql_calls) == 3  # ceil(5 / 2)
    # Every attempt's check runs, not only the latest attempt's.
    assert "checkType:ALL" in json.loads(graphql_calls[0][1])["query"]


def test_gateway_error_halves_the_batch(monkeypatch):
    monkeypatch.setattr(amr, "SUITE_BATCH", 8)
    monkeypatch.setattr(amr.time, "sleep", lambda _s: None)
    runs = [
        amr.to_run_record("o/r", "private", _run(i, "2026-09-30T10:00:00Z"))
        for i in range(8)
    ]
    checks = {
        f"CS_{i}": [_check("j", "2026-09-30T10:00:00Z", "2026-09-30T10:02:00Z")]
        for i in range(8)
    }
    jobs = amr.fetch_jobs(runs, FakeGh({}, checks, gateway_over=2))
    assert len(jobs) == 8


def test_suite_larger_than_one_page_falls_back_to_rest(monkeypatch):
    monkeypatch.setattr(amr, "CHECK_RUNS_PAGE", 2)
    run = amr.to_run_record("o/r", "private", _run(9, "2026-09-30T10:00:00Z"))
    checks = {
        "CS_9": [
            _check(f"c{i}", "2026-09-30T10:00:00Z", "2026-09-30T10:00:10Z")
            for i in range(3)
        ]
    }
    rest = {
        9: [
            {
                "name": f"c{i}",
                "started_at": "2026-09-30T10:00:00Z",
                "completed_at": "2026-09-30T10:00:10Z",
                "conclusion": "success",
            }
            for i in range(3)
        ]
    }
    jobs = amr.fetch_jobs([run], FakeGh({}, checks, rest_jobs=rest))
    assert sorted(j.job for j in jobs) == ["c0", "c1", "c2"]


def test_non_transient_failure_raises_without_retry():
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        return 1, "", "gh: HTTP 401: Bad credentials"

    with pytest.raises(amr.ReportError):
        amr.gh_json(["api", "x"], run, sleep=lambda _s: None)
    assert len(calls) == 1


def test_transient_failure_is_retried():
    answers = iter([(1, "", "gh: HTTP 503"), (0, '{"ok": 1}', "")])
    assert amr.gh_json(
        ["api", "x"], lambda a, s=None: next(answers), sleep=lambda _s: None
    ) == {"ok": 1}


# ── summary ─────────────────────────────────────────────────────────────────


def _fixture():
    """Two runs on a private repo: a fanned-out Conformance on a Renovate
    branch (pushed twice) and a merge-queue Tests run."""
    raw_runs = [
        _run(
            1,
            "2026-09-28T10:00:00Z",
            name="Conformance",
            branch="renovate/a",
            sha="s1",
            actor="atlan-app-fleet[bot]",
        ),
        _run(
            2,
            "2026-09-28T11:00:00Z",
            name="Conformance",
            branch="renovate/a",
            sha="s2",
            actor="atlan-app-fleet[bot]",
        ),
        _run(
            3,
            "2026-09-29T10:00:00Z",
            name="Tests",
            event="merge_group",
            branch="gh-readonly-queue/main/pr-1",
            actor="github-merge-queue[bot]",
        ),
    ]
    runs = [amr.to_run_record("o/r", "private", r) for r in raw_runs]
    short = ("2026-09-28T10:00:00Z", "2026-09-28T10:00:05Z")  # 5s -> 1 billed
    long = ("2026-09-29T10:00:00Z", "2026-09-29T10:05:00Z")  # 300s -> 5 billed
    jobs = []
    for run in runs[:2]:
        for name in ("suite / A", "suite / B", "suite / C"):
            jobs.append(amr._job_record(run, _check(name, *short)))
    jobs.append(amr._job_record(runs[2], _check("unit", *long)))
    return runs, jobs


def test_renovate_repush_counts_runs_after_the_first_sha():
    runs, _ = _fixture()
    assert amr.renovate_repush_runs(runs) == {("o/r", 2)}


def test_headline_shares():
    runs, jobs = _fixture()
    shares = amr.headline_shares(runs, jobs)
    assert shares["total"] == 11
    assert shares["Conformance workflows (name contains `conformance`)"] == 6
    assert shares["Renovate lane (`renovate/*` branches, all events)"] == 6
    assert shares["Renovate re-push runs (rebase/update; lower bound)"] == 3
    assert shares["Merge queue (`merge_group` event)"] == 5


def test_tally_ratio_shows_rounding_overhead():
    runs, jobs = _fixture()
    conf = amr.tally_by(runs, jobs, lambda x: x.workflow)["Conformance"]
    assert (conf.runs, conf.jobs, conf.wall_seconds, conf.billed_minutes) == (
        2,
        6,
        30,
        6,
    )
    assert conf.ratio == pytest.approx(12.0)


def test_job_level_tally_counts_distinct_runs():
    _, jobs = _fixture()
    t = amr.tally_by(None, jobs, lambda x: x.job)["suite / A"]
    assert (t.runs, t.jobs) == (2, 2)


def test_summary_contains_every_section_and_extrapolates():
    runs, jobs = _fixture()
    md = amr.render_summary(runs, jobs, "2026-09-28", "2026-10-04", 10, top=5)
    for heading in (
        "## Totals",
        "## Headline shares",
        "## Top 5 workflows",
        "## Top 5 jobs",
        "## By triggering event",
        "## By actor lane",
        "## Runs per repo per weekday",
    ):
        assert heading in md
    assert "| fleet estimate (×10.00) | 30 |" in md
    assert "| o/r | 2 | 1 | 0 | 0 | 0 | 0 | 0 | 3 |" in md
    assert "| Conformance | 2 | 6 | 0 | 6 | 54.5% | 12.00 |" in md


def test_main_writes_csvs_and_from_dir_rerenders_identically(tmp_path):
    raw = [_run(1, "2026-09-28T10:00:00Z", name="Conformance")]
    checks = {
        "CS_1": [_check("suite / A", "2026-09-28T10:00:00Z", "2026-09-28T10:00:07Z")]
    }
    gh = FakeGh({"o/r": raw}, checks)
    out = tmp_path / "out"
    assert (
        amr.main(
            [
                "--repo",
                "o/r",
                "--since",
                "2026-09-28",
                "--until",
                "2026-09-28",
                "--out-dir",
                str(out),
            ],
            run=gh,
        )
        == 0
    )
    first = (out / "summary.md").read_text()
    assert "jobs.csv" not in first and "| Conformance | 1 | 1 |" in first
    assert json.loads((out / "meta.json").read_text())["repos"] == ["o/r"]
    assert amr.main(["--from-dir", str(out)], run=gh) == 0
    assert (out / "summary.md").read_text() == first


def test_main_reports_api_failure_as_exit_1(tmp_path, capsys):
    def run(args, stdin=None):
        return 1, "", "gh: HTTP 401: Bad credentials"

    assert (
        amr.main(
            [
                "--repo",
                "o/r",
                "--since",
                "2026-09-28",
                "--until",
                "2026-09-28",
                "--out-dir",
                str(tmp_path),
            ],
            run=run,
        )
        == 1
    )
    assert "::error::" in capsys.readouterr().err
    assert not (tmp_path / "summary.md").exists()

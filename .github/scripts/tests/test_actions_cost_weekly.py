"""Tests for .github/scripts/actions_cost_weekly.py.

The scan is replaced through the `scan` seam by a function that writes the
same runs.csv / jobs.csv / meta.json the real report writes, and Slack through
the `post` seam, so nothing touches the network.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions_cost_weekly as acw  # noqa: E402
import actions_minutes_report as amr  # noqa: E402

# Monday 2026-10-12: the current week is 10-05..10-11, the previous 09-28..10-04.
TODAY = date(2026, 10, 12)
PREV_DAY = "2026-09-30"
CUR_DAY = "2026-10-07"
REPO = "atlanhq/atlan-example-app"


def _run(run_id: int, day: str, workflow: str, repo: str = REPO) -> amr.RunRecord:
    return amr.RunRecord(
        repo=repo,
        visibility="private",
        run_id=run_id,
        workflow=workflow,
        event="pull_request",
        lane="human",
        actor="dev",
        head_branch="feat/x",
        head_sha="a1",
        created_at=f"{day}T10:00:00Z",
        weekday="Wed",
        run_attempt=1,
        check_suite_node_id=f"CS_{run_id}",
    )


def _job(run: amr.RunRecord, job: str, minutes: int) -> amr.JobRecord:
    return amr.JobRecord(
        repo=run.repo,
        run_id=run.run_id,
        workflow=run.workflow,
        job=job,
        event=run.event,
        lane=run.lane,
        weekday=run.weekday,
        conclusion="success",
        started_at="",
        completed_at="",
        wall_seconds=minutes * 60,
        billed_minutes=minutes,
    )


def _usage(spec: dict) -> tuple:
    """runs, jobs from {(day, workflow, repo): minutes}, one run per entry."""
    runs, jobs = [], []
    for i, ((day, workflow, repo), minutes) in enumerate(sorted(spec.items()), 1):
        run = _run(i, day, workflow, repo)
        runs.append(run)
        jobs.append(_job(run, "build", minutes))
    return runs, jobs


def _write_report(report_dir: Path, runs: list, jobs: list, fleet_size=None) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    amr.write_csv(report_dir / "runs.csv", runs, amr.RunRecord)
    amr.write_csv(report_dir / "jobs.csv", jobs, amr.JobRecord)
    (report_dir / "meta.json").write_text(
        json.dumps(
            {"since": "2026-09-28", "until": "2026-10-11", "fleet_size": fleet_size}
        )
    )


class FakeSlack:
    def __init__(self, status: int = 200, error: Exception = None):
        self.status = status
        self.error = error
        self.posts = []

    def __call__(self, url: str, body: dict) -> int:
        self.posts.append((url, body))
        if self.error:
            raise self.error
        return self.status


WEBHOOK = "https://hooks.slack.com/services/T000/B000/placeholder"


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    for name in ("SLACK_ACTIONS_COST_WEBHOOK", "GITHUB_RUN_ID"):
        monkeypatch.delenv(name, raising=False)


def test_seams_default_to_real_implementations():
    params = inspect.signature(acw.main).parameters
    assert params["post"].default is acw._post_json
    assert params["scan"].default is acw._scan


# ── weeks ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "today", [date(2026, 10, 12), date(2026, 10, 15), date(2026, 10, 18)]
)
def test_weeks_are_the_last_two_complete_mon_sun_weeks(today):
    weeks = acw.weeks_before(today)
    assert weeks.previous_start == date(2026, 9, 28)
    assert weeks.previous_end == date(2026, 10, 4)
    assert weeks.current_start == date(2026, 10, 5)
    assert weeks.current_end == date(2026, 10, 11)


def test_meta_window_must_be_fourteen_days():
    with pytest.raises(ValueError, match="not 14 days"):
        acw.weeks_from_meta({"since": "2026-10-05", "until": "2026-10-11"})


def test_jobs_split_by_their_runs_creation_week():
    runs, jobs = _usage(
        {
            (PREV_DAY, "Tests", REPO): 10,
            (CUR_DAY, "Tests", REPO): 20,
            # Sunday 23:59 belongs to the previous week, Monday 00:00 to the current.
            ("2026-10-04", "Edge", REPO): 1,
            ("2026-10-05", "Edge", REPO): 2,
        }
    )
    previous, current = acw.split_weeks(runs, jobs, acw.weeks_before(TODAY))
    assert sorted(j.billed_minutes for j in previous) == [1, 10]
    assert sorted(j.billed_minutes for j in current) == [2, 20]


# ── regressions ─────────────────────────────────────────────────────────────


def _compare(spec: dict, **kw) -> acw.Comparison:
    runs, jobs = _usage(spec)
    return acw.compare(
        runs,
        jobs,
        acw.weeks_before(TODAY),
        kw.get("fleet_size"),
        kw.get("threshold", 0.25),
        kw.get("min_minutes", 300),
    )


def test_steady_week_has_no_regression():
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 1000,
            (CUR_DAY, "Tests", REPO): 1100,  # +10%
        }
    )
    assert c.regressions == []


def test_total_growth_over_threshold_alerts():
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 1000,
            (CUR_DAY, "Tests", REPO): 1260,  # +26%
        }
    )
    assert [d.name for d in c.regressions] == ["Total billed minutes", "Tests"]


def test_exactly_threshold_is_not_a_regression():
    c = _compare({(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 1250})
    assert c.regressions == []


def test_single_workflow_growth_alerts_when_total_is_flat():
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 10_000,
            (CUR_DAY, "Tests", REPO): 10_000,
            (PREV_DAY, "Conformance", REPO): 1_000,
            (CUR_DAY, "Conformance", REPO): 2_000,  # doubled
        }
    )
    assert [d.name for d in c.regressions] == ["Conformance"]


def test_small_workflow_below_floor_never_alerts():
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 10_000,
            (CUR_DAY, "Tests", REPO): 10_000,
            (PREV_DAY, "Stale", REPO): 4,
            (CUR_DAY, "Stale", REPO): 200,
        }
    )
    assert c.regressions == []


def test_new_workflow_above_floor_alerts():
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 10_000,
            (CUR_DAY, "Tests", REPO): 10_000,
            (CUR_DAY, "New Template", REPO): 500,
        }
    )
    assert [(d.name, d.change) for d in c.regressions] == [("New Template", None)]


def test_shrinking_workflow_never_alerts():
    c = _compare({(PREV_DAY, "Tests", REPO): 10_000, (CUR_DAY, "Tests", REPO): 100})
    assert c.regressions == []


# ── outputs ─────────────────────────────────────────────────────────────────


def test_summary_shows_deltas_and_fleet_estimate():
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 1000,
            (CUR_DAY, "Tests", REPO): 2000,
        },
        fleet_size=24,
    )
    md = acw.render_summary(c, top=5)
    assert "2026-10-05 → 2026-10-11" in md
    assert "| Tests | 1,000 | 2,000 | +100.0% |" in md
    assert "fleet estimate (×24.00) | 24,000 | 48,000 | +100.0% |" in md
    assert "regression(s)" in md


def test_dashboard_layout_matches_publish_fleet_dashboard(tmp_path):
    other = "atlanhq/atlan-other-app"
    c = _compare(
        {
            (PREV_DAY, "Tests", REPO): 100,
            (CUR_DAY, "Tests", REPO): 150,
            (CUR_DAY, "Tests", other): 30,
        }
    )
    acw.write_dashboard(c, tmp_path, top=5, collected_at="2026-10-12T03:00:00Z")

    assert sorted(p.name for p in (tmp_path / "repos").iterdir()) == [
        "atlanhq_atlan-example-app.json",
        "atlanhq_atlan-other-app.json",
    ]
    fleet = json.loads((tmp_path / "fleet.json").read_text())
    assert fleet["week"] == "2026-10-05"
    assert fleet["total"] == {
        "name": "Total billed minutes",
        "previous": 100,
        "current": 180,
        "change": 0.8,
    }
    # History is keyed by week so publish_fleet_dashboard's per-date dedupe
    # replaces a re-run's line instead of adding a second point.
    history = json.loads((tmp_path / "history_fleet.jsonl").read_text())
    assert history["date"] == "2026-10-05"
    assert history["billedMinutes"] == 180
    repo_history = json.loads(
        (tmp_path / "history_atlanhq_atlan-example-app.jsonl").read_text()
    )
    assert repo_history == {
        "date": "2026-10-05",
        "repo": REPO,
        "billedMinutes": 150,
        "workflows": {"Tests": 150},
    }


# ── Slack ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/x",
        "https://hooks.slack.com.attacker.example/services/x",
        "https://example.com/hooks.slack.com",
        "",
    ],
)
def test_non_slack_webhook_is_never_posted_to(url):
    c = _compare({(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 2000})
    slack = FakeSlack()
    assert acw.notify(c, url, None, post=slack) is False
    assert slack.posts == []


def test_slack_message_lists_regressions_and_run_link():
    c = _compare({(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 2000})
    slack = FakeSlack()
    assert acw.notify(c, WEBHOOK, "https://github.com/o/r/actions/runs/1", post=slack)
    text = slack.posts[0][1]["text"]
    assert "Tests: 1,000 → 2,000 billed min (+100.0%)" in text
    assert "<https://github.com/o/r/actions/runs/1|Full report>" in text
    # Aggregates only — the repo name stays in the step summary.
    assert REPO not in text


@pytest.mark.parametrize(
    "slack", [FakeSlack(status=500), FakeSlack(error=OSError("connection reset"))]
)
def test_slack_failure_is_reported_not_raised(slack):
    c = _compare({(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 2000})
    assert acw.notify(c, WEBHOOK, None, post=slack) is False


# ── main ────────────────────────────────────────────────────────────────────


def _scan_writing(spec: dict, seen: list):
    def scan(args, weeks, report_dir):
        seen.append(weeks)
        runs, jobs = _usage(spec)
        _write_report(report_dir, runs, jobs, fleet_size=10)
        return 0

    return scan


def test_main_quiet_week_exits_zero_without_posting(tmp_path):
    seen: list = []
    slack = FakeSlack()
    code = acw.main(
        ["--today", TODAY.isoformat(), "--out-dir", str(tmp_path)],
        post=slack,
        scan=_scan_writing(
            {(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 1000}, seen
        ),
    )
    assert code == 0
    assert slack.posts == []
    assert seen == [acw.weeks_before(TODAY)]
    assert (tmp_path / "summary.md").exists()
    assert (tmp_path / "fleet.json").exists()


def test_main_regression_posts_to_slack(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_ACTIONS_COST_WEBHOOK", WEBHOOK)
    slack = FakeSlack()
    code = acw.main(
        ["--today", TODAY.isoformat(), "--out-dir", str(tmp_path)],
        post=slack,
        scan=_scan_writing(
            {(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 2000}, []
        ),
    )
    assert code == 0
    assert len(slack.posts) == 1


def test_main_regression_without_webhook_fails_the_run(tmp_path):
    # An alert nobody receives must not look like a quiet week.
    code = acw.main(
        ["--today", TODAY.isoformat(), "--out-dir", str(tmp_path)],
        post=FakeSlack(),
        scan=_scan_writing(
            {(PREV_DAY, "Tests", REPO): 1000, (CUR_DAY, "Tests", REPO): 2000}, []
        ),
    )
    assert code == 1
    # The dashboard files are still written for the publish step.
    assert (tmp_path / "fleet.json").exists()


def test_main_scan_failure_propagates(tmp_path):
    code = acw.main(
        ["--today", TODAY.isoformat(), "--out-dir", str(tmp_path)],
        post=FakeSlack(),
        scan=lambda args, weeks, report_dir: 1,
    )
    assert code == 1
    assert not (tmp_path / "fleet.json").exists()


def test_main_report_dir_rerenders_without_scanning(tmp_path):
    runs, jobs = _usage({(PREV_DAY, "Tests", REPO): 10, (CUR_DAY, "Tests", REPO): 10})
    _write_report(tmp_path / "report", runs, jobs)

    def no_scan(args, weeks, report_dir):
        raise AssertionError("--report-dir must not scan")

    code = acw.main(
        ["--report-dir", str(tmp_path / "report"), "--out-dir", str(tmp_path / "o")],
        post=FakeSlack(),
        scan=no_scan,
    )
    assert code == 0
    assert "| Tests | 10 | 10 | +0.0% |" in (tmp_path / "o" / "summary.md").read_text()


def test_scan_requests_the_fourteen_day_window(tmp_path, monkeypatch):
    captured: list = []
    monkeypatch.setattr(amr, "main", lambda argv: captured.append(argv) or 0)
    args = acw.argparse.Namespace(
        owner="atlanhq", seed=3312, top=15, sample=12, repo=[]
    )
    assert acw._scan(args, acw.weeks_before(TODAY), tmp_path) == 0
    argv = captured[0]
    assert argv[argv.index("--since") + 1] == "2026-09-28"
    assert argv[argv.index("--until") + 1] == "2026-10-11"
    assert argv[argv.index("--sample") + 1] == "12"
    assert argv[argv.index("--seed") + 1] == "3312"

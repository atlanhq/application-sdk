#!/usr/bin/env python3
"""Weekly Actions billed-minutes report with a week-over-week regression alert.

A template change once doubled the fleet's Actions bill and nobody noticed for
two months, because nothing compared one week's minutes with the last. This
runs ``actions_minutes_report.py`` once a week and does that comparison.

One scan, two weeks
-------------------
Each run scans a single 14-day window (the two most recent complete Mon..Sun
UTC weeks) and splits it by run creation date. The previous week is not read
back from stored history, for three reasons:

* the two weeks cover the same repos, so the delta is like-for-like even when
  the seeded sample changes because the fleet gained or lost a repo;
* the first run already has a delta;
* a failed or skipped week leaves no gap in the comparison.

Doubling the window doubles the API calls. That is why the workflow scans a
seeded sample rather than the whole fleet. A template change lands in every
repo, so a sample still shows it.

What alerts
-----------
A regression is any of:

* the sample's total billed minutes grew more than ``--threshold`` (25%);
* any one workflow's billed minutes grew more than ``--threshold``.

The workflow check ignores a workflow whose current week is under
``--min-minutes``. Without that floor a workflow going from 4 minutes to 9
(+125%) would alert every week. A workflow with no minutes last week and at
least ``--min-minutes`` this week is a regression too: a new workflow is
exactly how a template change adds cost.

Outputs (``--out-dir``)
-----------------------
* ``summary.md`` — totals, top workflows and jobs with deltas, regressions;
* ``fleet.json`` + ``history_fleet.jsonl`` and ``repos/<slug>.json`` +
  ``history_<slug>.jsonl`` — the layout ``publish_fleet_dashboard.py`` uploads;
* ``report/`` — the raw ``runs.csv`` / ``jobs.csv`` from the scan.

``--report-dir`` re-renders from an earlier ``report/`` without API calls.

Slack
-----
When there is a regression and the webhook env var (``--slack-webhook-env``)
holds a ``https://hooks.slack.com/`` URL, the alert is posted there. The
message carries aggregate minutes and workflow names only, never repo data
beyond that. If there is a regression and no usable webhook, the script exits
1 so the run goes red. Otherwise a regression nobody is told about would look
the same as no regression.

Usage:
    actions_cost_weekly.py --today 2026-10-12 --sample 12 --seed 3312 \\
        --out-dir /tmp/actions-cost
    actions_cost_weekly.py --report-dir /tmp/actions-cost/report \\
        --out-dir /tmp/actions-cost
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions_minutes_report as amr  # noqa: E402

# Week-over-week growth above this fraction is a regression (the issue's 25%).
DEFAULT_THRESHOLD = 0.25

# A workflow under this many billed minutes in the current week is never
# flagged on its own. At the M1 sample's ~69k billed minutes a week this is
# under 0.5% of the bill, below anything worth paging about.
DEFAULT_MIN_MINUTES = 300

SLACK_HOST = "hooks.slack.com"
SLACK_TIMEOUT_SECONDS = 10

# (url, json body) -> HTTP status. Seam so tests never touch the network.
PostFn = Callable[[str, dict], int]


@dataclass(frozen=True)
class Weeks:
    """The two complete Mon..Sun UTC weeks before ``today``."""

    previous_start: date
    current_start: date
    current_end: date

    @property
    def previous_end(self) -> date:
        return self.current_start - timedelta(days=1)


@dataclass(frozen=True)
class Delta:
    name: str
    previous: int
    current: int

    @property
    def change(self) -> Optional[float]:
        """Fractional growth; None when last week had nothing to grow from."""
        if self.previous == 0:
            return None
        return (self.current - self.previous) / self.previous


def weeks_before(today: date) -> Weeks:
    """The last two complete weeks. A Monday run reports the week that ended
    yesterday; any other day reports the last week that has fully ended."""
    current_start = today - timedelta(days=today.weekday() + 7)
    return Weeks(
        previous_start=current_start - timedelta(days=7),
        current_start=current_start,
        current_end=current_start + timedelta(days=6),
    )


def weeks_from_meta(meta: dict) -> Weeks:
    """The two weeks of an earlier 14-day scan, read from its ``meta.json``."""
    since = date.fromisoformat(meta["since"])
    until = date.fromisoformat(meta["until"])
    if (until - since).days != 13:
        raise ValueError(
            f"report window {since}..{until} is not 14 days; cannot split it "
            "into two weeks"
        )
    return Weeks(
        previous_start=since,
        current_start=since + timedelta(days=7),
        current_end=until,
    )


def split_weeks(runs: list, jobs: list, weeks: Weeks) -> tuple:
    """(previous jobs, current jobs), bucketed by their run's creation date.

    A job belongs to the week its run was created in, the same rule the scan
    used to pick runs, so a run that straddles midnight on Sunday is counted
    once, in one week.
    """
    created = {(r.repo, r.run_id): amr.parse_ts(r.created_at).date() for r in runs}
    previous, current = [], []
    for job in jobs:
        day = created.get((job.repo, job.run_id))
        if day is None:
            continue
        if weeks.previous_start <= day <= weeks.previous_end:
            previous.append(job)
        elif weeks.current_start <= day <= weeks.current_end:
            current.append(job)
    return previous, current


def billed_by(jobs: list, key: Callable) -> dict:
    out: dict = defaultdict(int)
    for job in jobs:
        out[key(job)] += job.billed_minutes
    return dict(out)


def deltas(previous: dict, current: dict) -> list:
    """One Delta per key in either week, largest current week first."""
    names = set(previous) | set(current)
    return sorted(
        (Delta(n, previous.get(n, 0), current.get(n, 0)) for n in names),
        key=lambda d: (-d.current, -d.previous, d.name),
    )


def is_regression(delta: Delta, threshold: float, min_minutes: int) -> bool:
    if delta.current < min_minutes:
        return False
    change = delta.change
    return change is None or change > threshold


def find_regressions(
    total: Delta, workflows: list, threshold: float, min_minutes: int
) -> list:
    """The deltas that alert: the total first, then workflows."""
    found = []
    # The total has no floor: a sample whose whole bill grew 25% is news at
    # any size. A total that went from nothing to something is not a growth
    # figure, so it is only flagged once there is a baseline.
    if total.change is not None and total.change > threshold:
        found.append(total)
    found += [w for w in workflows if is_regression(w, threshold, min_minutes)]
    return found


def _fmt_change(delta: Delta) -> str:
    change = delta.change
    if change is None:
        return "new" if delta.current else "—"
    return f"{change * 100:+.1f}%"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _delta_table(title: str, rows: list, top: int) -> list:
    lines = [
        f"| {title} | previous week | current week | change |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| {_cell(d.name)} | {d.previous:,} | {d.current:,} | {_fmt_change(d)} |"
        for d in rows[:top]
    ]
    return lines


@dataclass(frozen=True)
class Comparison:
    weeks: Weeks
    repos: list
    fleet_size: Optional[int]
    total: Delta
    workflows: list
    jobs: list
    per_repo: dict
    regressions: list
    threshold: float
    min_minutes: int

    @property
    def fleet_factor(self) -> Optional[float]:
        if not self.fleet_size or not self.repos:
            return None
        return self.fleet_size / len(self.repos)


def compare(
    runs: list,
    jobs: list,
    weeks: Weeks,
    fleet_size: Optional[int],
    threshold: float,
    min_minutes: int,
) -> Comparison:
    previous, current = split_weeks(runs, jobs, weeks)
    workflows = deltas(
        billed_by(previous, lambda j: j.workflow),
        billed_by(current, lambda j: j.workflow),
    )
    job_rows = deltas(
        billed_by(previous, lambda j: f"{j.workflow} / {j.job}"),
        billed_by(current, lambda j: f"{j.workflow} / {j.job}"),
    )
    total = Delta(
        "Total billed minutes",
        sum(j.billed_minutes for j in previous),
        sum(j.billed_minutes for j in current),
    )
    repos = sorted({r.repo for r in runs})
    prev_repo = billed_by(previous, lambda j: j.repo)
    cur_repo = billed_by(current, lambda j: j.repo)
    per_repo = {
        repo: {
            "total": Delta(repo, prev_repo.get(repo, 0), cur_repo.get(repo, 0)),
            "workflows": deltas(
                billed_by(
                    [j for j in previous if j.repo == repo], lambda j: j.workflow
                ),
                billed_by([j for j in current if j.repo == repo], lambda j: j.workflow),
            ),
        }
        for repo in repos
    }
    return Comparison(
        weeks=weeks,
        repos=repos,
        fleet_size=fleet_size,
        total=total,
        workflows=workflows,
        jobs=job_rows,
        per_repo=per_repo,
        regressions=find_regressions(total, workflows, threshold, min_minutes),
        threshold=threshold,
        min_minutes=min_minutes,
    )


def render_summary(c: Comparison, top: int) -> str:
    w = c.weeks
    lines = [
        f"# Weekly Actions billed minutes — {w.current_start} → {w.current_end} (UTC)",
        "",
        f"Compared with {w.previous_start} → {w.previous_end}. Repos scanned: "
        f"**{len(c.repos)}**"
        + (f" of a {c.fleet_size}-repo fleet" if c.fleet_size else "")
        + ". *Billed* = each job's wall-clock rounded up to a whole minute; OS "
        "multipliers and the public-repo exemption are not applied.",
        "",
    ]
    if c.regressions:
        lines += [
            f"## :rotating_light: {len(c.regressions)} regression(s) over "
            f"{c.threshold * 100:.0f}% week over week",
            "",
            *_delta_table("what grew", c.regressions, len(c.regressions)),
            "",
        ]
    else:
        lines += [
            f"No regression: neither the total nor any workflow with at least "
            f"{c.min_minutes:,} billed minutes grew more than "
            f"{c.threshold * 100:.0f}%.",
            "",
        ]
    lines += ["## Totals", "", *_delta_table("scope", [c.total], 1)]
    factor = c.fleet_factor
    if factor:
        lines.append(
            f"| fleet estimate (×{factor:.2f}) | {c.total.previous * factor:,.0f} | "
            f"{c.total.current * factor:,.0f} | {_fmt_change(c.total)} |"
        )
    lines += [
        "",
        f"## Top {top} workflows",
        "",
        *_delta_table("workflow", c.workflows, top),
        "",
        f"## Top {top} jobs",
        "",
        *_delta_table("workflow / job", c.jobs, top),
        "",
        "## Per repo",
        "",
        *_delta_table(
            "repo",
            sorted(
                (v["total"] for v in c.per_repo.values()),
                key=lambda d: (-d.current, d.name),
            ),
            len(c.per_repo),
        ),
    ]
    return "\n".join(lines) + "\n"


def _delta_doc(d: Delta) -> dict:
    return {
        "name": d.name,
        "previous": d.previous,
        "current": d.current,
        "change": d.change,
    }


def write_dashboard(c: Comparison, out_dir: Path, top: int, collected_at: str) -> None:
    """``repos/<slug>.json``, ``fleet.json`` and history in the layout
    ``publish_fleet_dashboard.py`` uploads.

    History is keyed by the current week's Monday, so a re-run for the same
    week replaces its line instead of adding a second point.
    """
    week = c.weeks.current_start.isoformat()
    (out_dir / "repos").mkdir(parents=True, exist_ok=True)
    for repo, data in c.per_repo.items():
        slug = repo.replace("/", "_")
        doc = {
            "repo": repo,
            "collectedAt": collected_at,
            "week": week,
            "total": _delta_doc(data["total"]),
            "workflows": [_delta_doc(d) for d in data["workflows"]],
        }
        (out_dir / "repos" / f"{slug}.json").write_text(json.dumps(doc, indent=2))
        entry = {
            "date": week,
            "repo": repo,
            "billedMinutes": data["total"].current,
            "workflows": {d.name: d.current for d in data["workflows"] if d.current},
        }
        (out_dir / f"history_{slug}.jsonl").write_text(json.dumps(entry) + "\n")

    fleet = {
        "collectedAt": collected_at,
        "week": week,
        "previousWeek": c.weeks.previous_start.isoformat(),
        "reposScanned": len(c.repos),
        "fleetSize": c.fleet_size,
        "fleetFactor": c.fleet_factor,
        "threshold": c.threshold,
        "minMinutes": c.min_minutes,
        "total": _delta_doc(c.total),
        "topWorkflows": [_delta_doc(d) for d in c.workflows[:top]],
        "topJobs": [_delta_doc(d) for d in c.jobs[:top]],
        "regressions": [_delta_doc(d) for d in c.regressions],
    }
    (out_dir / "fleet.json").write_text(json.dumps(fleet, indent=2))
    fleet_entry = {
        "date": week,
        "reposScanned": len(c.repos),
        "fleetSize": c.fleet_size,
        "billedMinutes": c.total.current,
        "workflows": {d.name: d.current for d in c.workflows if d.current},
        "regressions": len(c.regressions),
    }
    (out_dir / "history_fleet.jsonl").write_text(json.dumps(fleet_entry) + "\n")


# ── Slack ───────────────────────────────────────────────────────────────────


def slack_message(c: Comparison, run_url: Optional[str]) -> dict:
    w = c.weeks
    lines = [
        f":rotating_light: *Actions billed minutes grew more than "
        f"{c.threshold * 100:.0f}% week over week* "
        f"({w.current_start} → {w.current_end} vs {w.previous_start} → "
        f"{w.previous_end}, {len(c.repos)} repos sampled)",
    ]
    lines += [
        f"• {d.name}: {d.previous:,} → {d.current:,} billed min ({_fmt_change(d)})"
        for d in c.regressions
    ]
    if run_url:
        lines.append(f"<{run_url}|Full report>")
    return {"text": "\n".join(lines)}


def valid_webhook(url: str) -> bool:
    """Only an https hooks.slack.com URL is posted to. A misconfigured secret
    pointing anywhere else must not receive the report."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == SLACK_HOST


def _post_json(url: str, body: dict) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS) as resp:
        return int(resp.status)


def notify(
    c: Comparison, webhook: str, run_url: Optional[str], post: PostFn = _post_json
) -> bool:
    """Post the regression alert. True when Slack accepted it.

    Never raises: a Slack outage must not hide the report, which is still in
    the step summary. The caller turns False into a red run.
    """
    if not webhook:
        print("::warning::Slack webhook not set — regression alert not sent")
        return False
    if not valid_webhook(webhook):
        print(
            f"::warning::Slack webhook is not an https://{SLACK_HOST}/ URL — "
            "refusing to send"
        )
        return False
    try:
        status = post(webhook, slack_message(c, run_url))
    except OSError as exc:
        print(f"::warning::Slack post failed: {exc}")
        return False
    if status >= 300:
        print(f"::warning::Slack post returned HTTP {status}")
        return False
    return True


# ── entrypoint ──────────────────────────────────────────────────────────────


def _scan(args: argparse.Namespace, weeks: Weeks, report_dir: Path) -> int:
    argv = [
        "--owner",
        args.owner,
        "--since",
        weeks.previous_start.isoformat(),
        "--until",
        weeks.current_end.isoformat(),
        "--seed",
        str(args.seed),
        "--top",
        str(args.top),
        "--out-dir",
        str(report_dir),
    ]
    if args.sample is not None:
        argv += ["--sample", str(args.sample)]
    for repo in args.repo:
        argv += ["--repo", repo]
    return amr.main(argv)


def main(
    argv: Optional[list] = None,
    post: PostFn = _post_json,
    scan: Callable[[argparse.Namespace, Weeks, Path], int] = _scan,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--owner", default="atlanhq")
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="UTC date the report runs on (default: today); the two complete "
        "weeks before it are compared",
    )
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/REPO")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-minutes", type=int, default=DEFAULT_MIN_MINUTES)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="re-use an earlier 14-day scan instead of calling the API",
    )
    parser.add_argument(
        "--slack-webhook-env",
        default="SLACK_ACTIONS_COST_WEBHOOK",
        help="name of the env var holding the Slack incoming-webhook URL",
    )
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    report_dir = args.report_dir
    if report_dir is None:
        today = args.today or datetime.now(timezone.utc).date()
        report_dir = args.out_dir / "report"
        code = scan(args, weeks_before(today), report_dir)
        if code != 0:
            return code

    meta = json.loads((report_dir / "meta.json").read_text())
    weeks = weeks_from_meta(meta)
    runs = amr.read_csv(report_dir / "runs.csv", amr.RunRecord)
    jobs = amr.read_csv(report_dir / "jobs.csv", amr.JobRecord)
    comparison = compare(
        runs, jobs, weeks, meta.get("fleet_size"), args.threshold, args.min_minutes
    )

    summary = render_summary(comparison, args.top)
    (args.out_dir / "summary.md").write_text(summary)
    print(summary)
    write_dashboard(
        comparison,
        args.out_dir,
        args.top,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    if not comparison.regressions:
        return 0
    for d in comparison.regressions:
        print(
            f"::warning::{d.name}: {d.previous:,} → {d.current:,} billed minutes "
            f"({_fmt_change(d)})"
        )
    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
            f"{os.environ['GITHUB_RUN_ID']}"
        )
    webhook = os.environ.get(args.slack_webhook_env, "").strip()
    return 0 if notify(comparison, webhook, run_url, post=post) else 1


if __name__ == "__main__":
    sys.exit(main())

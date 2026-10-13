#!/usr/bin/env python3
"""Per-workflow / per-job billed-minutes report across the connector fleet.

The billing view breaks Actions usage down by repo and SKU only, so it cannot
say *which workflow or job* the minutes go to. This script rebuilds the number
bottom-up from the run and job records, so every CI change can be measured
before and after on the same footing.

For a UTC date window it:

1. picks the repos — the fleet from ``discover_org_consumers`` (atlan-*-app
   repos extending the shared Renovate preset), an explicit ``--repo`` list, or
   a deterministic ``--sample`` of the fleet;
2. lists every workflow run created in the window, one UTC day at a time. The
   runs endpoint stops at 1000 results per query (``total_count`` keeps
   counting, the pages just end), so a window whose ``total_count`` exceeds the
   cap is split in half until each piece fits;
3. reads each run's jobs as the check runs of its check suite, batched through
   GraphQL ``nodes(ids:)`` — about one rate-limit point per batch, against one
   REST ``/jobs`` call per run. A suite with more check runs than one page
   holds falls back to REST ``/jobs?filter=all`` for that run;
4. computes per job the wall-clock seconds and the **billed** minutes, which is
   the wall-clock rounded up to a whole minute *per job*. A job that never ran
   (skipped, zero duration) bills nothing.

It then groups by workflow, job, triggering event, actor lane and weekday, and
writes ``runs.csv``, ``jobs.csv`` and ``summary.md`` into ``--out-dir``.
``--from-dir`` re-renders the summary from those CSVs without touching the API,
so a baseline can be re-cut (different top-N, new headline) after the fact.

Actor lane is derived from the head branch before the login, because the
self-hosted Renovate runner and the version-bump workflow both act as the fleet
App (``atlan-app-fleet[bot]``), not as ``renovate[bot]``:

    renovate/*        -> renovate
    bump-version-*    -> bump-version
    login without [bot] -> human
    any other [bot]   -> other-bot

What the billed figure does NOT model, so read it as "minutes the rounding rule
charges", not as dollars: the per-OS multipliers and larger-runner SKUs (check
runs carry no runner labels), and the fact that standard runners on *public*
repos are free. ``runs.csv`` records each repo's visibility so the summary can
split private from public.

Environment:
    GH_TOKEN   token for `gh` with actions:read on the repos in scope

Usage:
    actions_minutes_report.py --owner atlanhq --since 2026-09-28 --until 2026-10-04 \\
        --sample 12 --seed 3312 --out-dir /tmp/minutes
    actions_minutes_report.py --from-dir /tmp/minutes --top 20
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover_org_consumers as discover  # noqa: E402

# (args, stdin) -> (returncode, stdout, stderr). The same uninterpreting seam as
# discover_org_consumers._run_gh, widened by stdin so a GraphQL body can be
# passed with `--input -` instead of through argv.
RunFn = Callable[[list, Optional[str]], tuple]

# The runs endpoint returns at most this many results per query, however many
# pages are requested; past it the pages come back empty while total_count
# still reports the true size. A window must be split until it fits.
RUNS_RESULT_CAP = 1000
RUNS_PAGE_SIZE = 100

# Check suites per GraphQL query. Each suite carries up to CHECK_RUNS_PAGE check
# runs, and GitHub answers a query whose node product is too large with a
# 502/504 rather than a cost error (measured in renovate_fleet_scan.py), so the
# batch is halved on a gateway error rather than fixed at the largest that
# happened to work once.
SUITE_BATCH = 25
CHECK_RUNS_PAGE = 100

GH_RETRIES = 4
# One `gh` call is a single API request; two minutes is far past any healthy
# response, so a call still running then is a stalled connection.
GH_TIMEOUT_SECONDS = 120

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Events the headline groups call out by name; anything else is still counted,
# under its own event name.
HEADLINE_EVENTS = ("pull_request", "merge_group", "schedule", "workflow_run", "push")


class Lane(str, Enum):
    """Who caused a run, at the granularity a CI-cost decision needs."""

    RENOVATE = "renovate"
    BUMP_VERSION = "bump-version"
    HUMAN = "human"
    OTHER_BOT = "other-bot"


class ReportError(RuntimeError):
    """The API would not answer; a partial report would understate the cost."""


@dataclass(frozen=True)
class RunRecord:
    repo: str
    visibility: str
    run_id: int
    workflow: str
    event: str
    lane: str
    actor: str
    head_branch: str
    head_sha: str
    created_at: str
    weekday: str
    run_attempt: int
    check_suite_node_id: str


@dataclass(frozen=True)
class JobRecord:
    repo: str
    run_id: int
    workflow: str
    job: str
    event: str
    lane: str
    weekday: str
    conclusion: str
    started_at: str
    completed_at: str
    wall_seconds: int
    billed_minutes: int


def _run_gh(args: list, stdin: Optional[str] = None) -> tuple:
    """Run `gh`, returning (returncode, stdout, stderr) uninterpreted.

    A call that outlives GH_TIMEOUT_SECONDS is reported as a failed call whose
    stderr says so, which gh_json treats as transient.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            input=stdin,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {GH_TIMEOUT_SECONDS}s"
    return result.returncode, result.stdout, result.stderr


def _is_transient(stderr: str) -> bool:
    """A failure worth retrying: gateway errors, secondary rate limits and
    stalled calls."""
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in (
            "http 502",
            "http 503",
            "http 504",
            "secondary rate limit",
            "timed out",
        )
    )


def _is_gateway(stderr: str) -> bool:
    lowered = stderr.lower()
    return "http 502" in lowered or "http 504" in lowered


# gh_json's default backoff sleeper. A module attribute so tests can stub this
# module's waits without patching `time.sleep` for the whole process.
_sleep: Callable[[float], None] = time.sleep


def _total_count(payload: dict, key: str, what: str) -> int:
    """The response's result count. Paging stops on it, so a response without
    one fails the report instead of being read as zero and truncated."""
    if key not in payload:
        raise ReportError(f"{what} is missing {key}")
    return int(payload[key])


def _is_primary_rate_limit(stderr: str) -> bool:
    """The hourly quota is spent. Unlike a secondary limit, a backoff of
    seconds can't outlast it, so it is reported rather than retried."""
    return "api rate limit exceeded" in stderr.lower()


def gh_json(
    args: list,
    run: RunFn,
    stdin: Optional[str] = None,
    sleep: Optional[Callable[[float], None]] = None,
    retries: int = GH_RETRIES,
    retry_gateway: bool = True,
):
    """`gh` call parsed as JSON, retrying transient failures with backoff.

    With retry_gateway=False a 502/504 fails at once, so a caller that can
    shrink the request can do that instead of resending it unchanged.
    """
    sleep = sleep or _sleep
    stderr = ""
    for attempt in range(retries):
        code, out, stderr = run(args, stdin)
        if code == 0:
            return json.loads(out)
        if _is_primary_rate_limit(stderr):
            raise ReportError(
                f"gh {' '.join(args[:2])} hit the primary API rate limit; "
                f"re-run after it resets (`gh api rate_limit`): {stderr.strip()}"
            )
        if (
            not _is_transient(stderr)
            or (not retry_gateway and _is_gateway(stderr))
            or attempt == retries - 1
        ):
            break
        sleep(2**attempt * 5)
    raise ReportError(f"gh {' '.join(args[:2])} failed: {stderr.strip()}")


# ── classification ──────────────────────────────────────────────────────────


def classify_lane(head_branch: str, actor: str) -> Lane:
    """Actor lane of a run — branch first, because the fleet App wears both
    the Renovate and the version-bump hats."""
    branch = head_branch or ""
    if branch.startswith("renovate/"):
        return Lane.RENOVATE
    if branch.startswith("bump-version-"):
        return Lane.BUMP_VERSION
    if actor.endswith("[bot]"):
        return Lane.OTHER_BOT
    return Lane.HUMAN


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_minutes(started_at: Optional[str], completed_at: Optional[str]) -> tuple:
    """(wall_seconds, billed_minutes) for one job.

    Billing rounds each job up to the next whole minute, so a 4-second job
    bills one minute. A job without both timestamps, or with zero duration
    (skipped, cancelled before it started), bills nothing.
    """
    if not started_at or not completed_at:
        return 0, 0
    seconds = int((parse_ts(completed_at) - parse_ts(started_at)).total_seconds())
    if seconds <= 0:
        return 0, 0
    return seconds, math.ceil(seconds / 60)


def is_conformance(workflow: str) -> bool:
    return "conformance" in workflow.lower()


# ── fetch ───────────────────────────────────────────────────────────────────


def day_windows(since: date, until: date) -> list:
    """Inclusive UTC [start, end] per day from since to until."""
    if until < since:
        raise ValueError(f"--until {until} is before --since {since}")
    windows = []
    day = since
    while day <= until:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        windows.append((start, start + timedelta(days=1) - timedelta(seconds=1)))
        day += timedelta(days=1)
    return windows


def _runs_page(repo: str, start: datetime, end: datetime, page: int, run: RunFn):
    return gh_json(
        [
            "api",
            "-X",
            "GET",
            f"repos/{repo}/actions/runs",
            "-f",
            f"created={fmt_ts(start)}..{fmt_ts(end)}",
            "-f",
            f"per_page={RUNS_PAGE_SIZE}",
            "-f",
            f"page={page}",
        ],
        run,
    )


def list_runs_window(repo: str, start: datetime, end: datetime, run: RunFn) -> list:
    """Every run created in [start, end], splitting the window under the cap.

    Raises ReportError if a one-second window still exceeds the cap, since
    that cannot be split further and paging it would silently truncate.
    """
    first = _runs_page(repo, start, end, 1, run)
    total = _total_count(first, "total_count", f"{repo}: runs response")
    if total > RUNS_RESULT_CAP:
        if end <= start:
            raise ReportError(
                f"{repo}: {total} runs created at {fmt_ts(start)} exceed the "
                f"{RUNS_RESULT_CAP}-result cap and the window cannot be split"
            )
        mid = start + (end - start) / 2
        mid = mid.replace(microsecond=0)
        return list_runs_window(repo, start, mid, run) + list_runs_window(
            repo, mid + timedelta(seconds=1), end, run
        )
    runs = list(first.get("workflow_runs") or [])
    pages = math.ceil(total / RUNS_PAGE_SIZE)
    for page in range(2, pages + 1):
        batch = _runs_page(repo, start, end, page, run).get("workflow_runs") or []
        if not batch:
            break
        runs.extend(batch)
    return runs


def repo_visibility(repo: str, run: RunFn) -> str:
    return str(
        gh_json(["api", f"repos/{repo}", "--jq", "{visibility}"], run).get(
            "visibility", "unknown"
        )
    )


def to_run_record(repo: str, visibility: str, raw: dict) -> RunRecord:
    actor = ((raw.get("triggering_actor") or raw.get("actor")) or {}).get("login", "")
    created = raw["created_at"]
    head_branch = raw.get("head_branch") or ""
    return RunRecord(
        repo=repo,
        visibility=visibility,
        run_id=int(raw["id"]),
        workflow=raw.get("name") or raw.get("path") or "",
        event=raw.get("event") or "",
        lane=classify_lane(head_branch, actor).value,
        actor=actor,
        head_branch=head_branch,
        head_sha=raw.get("head_sha") or "",
        created_at=created,
        weekday=WEEKDAYS[parse_ts(created).weekday()],
        run_attempt=int(raw.get("run_attempt") or 1),
        check_suite_node_id=raw.get("check_suite_node_id") or "",
    )


def list_runs(repo: str, since: date, until: date, run: RunFn) -> list:
    """RunRecords for every run created in the window, deduplicated by id."""
    visibility = repo_visibility(repo, run)
    seen = {}
    for start, end in day_windows(since, until):
        for raw in list_runs_window(repo, start, end, run):
            seen[int(raw["id"])] = to_run_record(repo, visibility, raw)
    return sorted(seen.values(), key=lambda r: (r.created_at, r.run_id))


_SUITES_QUERY = (
    "query($ids:[ID!]!){ nodes(ids:$ids){ ... on CheckSuite { id "
    f"checkRuns(first:{CHECK_RUNS_PAGE}, filterBy:{{checkType:ALL}}){{ totalCount "
    "nodes{ name startedAt completedAt conclusion } } } } }"
)


def _rest_jobs(repo: str, run_id: int, run: RunFn) -> list:
    """All attempts' jobs of one run via REST, for suites too big for one page."""
    jobs = []
    page = 1
    while True:
        data = gh_json(
            [
                "api",
                "-X",
                "GET",
                f"repos/{repo}/actions/runs/{run_id}/jobs",
                "-f",
                "filter=all",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
            ],
            run,
        )
        batch = data.get("jobs") or []
        jobs.extend(
            {
                "name": j.get("name", ""),
                "startedAt": j.get("started_at"),
                "completedAt": j.get("completed_at"),
                "conclusion": (j.get("conclusion") or "").upper(),
            }
            for j in batch
        )
        total = _total_count(data, "total_count", f"{repo}: run {run_id} jobs response")
        if not batch or len(jobs) >= total:
            return jobs
        page += 1


def _query_suites(ids: list, run: RunFn) -> dict:
    """{suite id: check-run payload}, halving the batch on a gateway error."""
    body = json.dumps({"query": _SUITES_QUERY, "variables": {"ids": ids}})
    try:
        # A multi-suite batch that hits a gateway error is split rather than
        # retried: the error is about the batch's size, so the same query
        # would fail again. Every other transient failure is retried as usual.
        data = gh_json(
            ["api", "graphql", "--input", "-"],
            run,
            stdin=body,
            retry_gateway=len(ids) == 1,
        )
    except ReportError as exc:
        if len(ids) > 1 and _is_gateway(str(exc)):
            half = len(ids) // 2
            return {**_query_suites(ids[:half], run), **_query_suites(ids[half:], run)}
        raise
    return {
        node["id"]: node["checkRuns"]
        for node in (data.get("data") or {}).get("nodes") or []
        if node and "checkRuns" in node
    }


def fetch_jobs(runs: list, run: RunFn) -> list:
    """JobRecords for every run, GraphQL-batched by check suite."""
    by_suite = {r.check_suite_node_id: r for r in runs if r.check_suite_node_id}
    suite_ids = list(by_suite)
    jobs = []
    for i in range(0, len(suite_ids), SUITE_BATCH):
        payloads = _query_suites(suite_ids[i : i + SUITE_BATCH], run)
        for suite_id, payload in payloads.items():
            owner = by_suite[suite_id]
            nodes = payload.get("nodes") or []
            total = _total_count(payload, "totalCount", f"check suite {suite_id}")
            if total > len(nodes):
                nodes = _rest_jobs(owner.repo, owner.run_id, run)
            jobs.extend(_job_record(owner, node) for node in nodes)
    return jobs


def _job_record(owner: RunRecord, node: dict) -> JobRecord:
    wall, billed = job_minutes(node.get("startedAt"), node.get("completedAt"))
    return JobRecord(
        repo=owner.repo,
        run_id=owner.run_id,
        workflow=owner.workflow,
        job=node.get("name") or "",
        event=owner.event,
        lane=owner.lane,
        weekday=owner.weekday,
        conclusion=(node.get("conclusion") or "").lower(),
        started_at=node.get("startedAt") or "",
        completed_at=node.get("completedAt") or "",
        wall_seconds=wall,
        billed_minutes=billed,
    )


def select_repos(fleet: list, explicit: list, sample: Optional[int], seed: int) -> list:
    """The repos to scan: explicit list wins, else a seeded sample, else all."""
    if explicit:
        return sorted(explicit)
    if sample is not None and sample < len(fleet):
        return sorted(random.Random(seed).sample(sorted(fleet), sample))
    return sorted(fleet)


# ── CSV round-trip ──────────────────────────────────────────────────────────


def write_csv(path: Path, rows: Iterable, cls) -> None:
    names = [f.name for f in fields(cls)]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def read_csv(path: Path, cls) -> list:
    types = {f.name: f.type for f in fields(cls)}
    rows = []
    with path.open(newline="") as fh:
        for raw in csv.DictReader(fh):
            rows.append(
                cls(
                    **{
                        k: int(v) if types[k] in (int, "int") else v
                        for k, v in raw.items()
                    }
                )
            )
    return rows


# ── summary ─────────────────────────────────────────────────────────────────


@dataclass
class Tally:
    runs: int = 0
    jobs: int = 0
    wall_seconds: int = 0
    billed_minutes: int = 0

    @property
    def wall_minutes(self) -> float:
        return self.wall_seconds / 60

    @property
    def ratio(self) -> float:
        """Billed per used minute; >1 is the per-job rounding overhead."""
        return self.billed_minutes / self.wall_minutes if self.wall_seconds else 0.0


def tally_by(runs: Optional[list], jobs: list, key: Callable) -> dict:
    """Tallies per key. With ``runs=None`` (a job-level key, which no run
    carries) the run count is the distinct runs among that key's jobs."""
    out: dict = defaultdict(Tally)
    run_ids: dict = defaultdict(set)
    for r in runs or []:
        out[key(r)].runs += 1
    for j in jobs:
        k = key(j)
        t = out[k]
        t.jobs += 1
        t.wall_seconds += j.wall_seconds
        t.billed_minutes += j.billed_minutes
        run_ids[k].add((j.repo, j.run_id))
    if runs is None:
        for k, ids in run_ids.items():
            out[k].runs = len(ids)
    return dict(out)


def renovate_repush_runs(runs: list) -> set:
    """(repo, run id) of Renovate pull_request runs on a head SHA that is not the
    branch's first SHA in the window — i.e. runs caused by Renovate pushing the
    branch again (a rebase onto a moved base, or a newer version).

    A lower bound: a branch whose first push predates the window has its
    earliest in-window SHA counted as the initial push.
    """
    first_sha: dict = {}
    repush = set()
    for r in sorted(runs, key=lambda r: (r.created_at, r.run_id)):
        if r.lane != Lane.RENOVATE.value or r.event != "pull_request":
            continue
        key = (r.repo, r.head_branch)
        first_sha.setdefault(key, r.head_sha)
        if r.head_sha != first_sha[key]:
            repush.add((r.repo, r.run_id))
    return repush


def headline_shares(runs: list, jobs: list) -> dict:
    """Billed minutes behind each figure the Slack breakdown quoted, plus the
    ``total`` they are shares of."""
    repush = renovate_repush_runs(runs)
    return {
        "total": sum(j.billed_minutes for j in jobs),
        "Conformance workflows (name contains `conformance`)": sum(
            j.billed_minutes for j in jobs if is_conformance(j.workflow)
        ),
        "Renovate lane (`renovate/*` branches, all events)": sum(
            j.billed_minutes for j in jobs if j.lane == Lane.RENOVATE.value
        ),
        "Renovate re-push runs (rebase/update; lower bound)": sum(
            j.billed_minutes for j in jobs if (j.repo, j.run_id) in repush
        ),
        "Merge queue (`merge_group` event)": sum(
            j.billed_minutes for j in jobs if j.event == "merge_group"
        ),
    }


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _row(label: str, t: Tally, total_billed: int) -> str:
    return (
        f"| {label} | {t.runs:,} | {t.jobs:,} | {t.wall_minutes:,.0f} | "
        f"{t.billed_minutes:,} | {_pct(t.billed_minutes, total_billed)} | "
        f"{t.ratio:.2f} |"
    )


_HEADER = (
    "| {0} | runs | jobs | used min | billed min | share of billed | billed/used |\n"
    "|---|---:|---:|---:|---:|---:|---:|"
)


def _table(title: str, tallies: dict, total: int, top: Optional[int]) -> list:
    ordered = sorted(tallies.items(), key=lambda kv: (-kv[1].billed_minutes, kv[0]))
    if top is not None:
        ordered = ordered[:top]
    return [_HEADER.format(title)] + [
        _row(str(k).replace("|", "\\|"), t, total) for k, t in ordered
    ]


def render_summary(
    runs: list,
    jobs: list,
    since: str,
    until: str,
    fleet_size: Optional[int],
    top: int,
) -> str:
    repos = sorted({r.repo for r in runs})
    total = tally_by(runs, jobs, lambda _: "all").get("all", Tally())
    billed = total.billed_minutes
    lines = [
        f"# Actions billed-minutes report — {since} → {until} (UTC)",
        "",
        f"Repos scanned: **{len(repos)}**"
        + (f" of a {fleet_size}-repo fleet" if fleet_size else ""),
        "",
        "*Billed* = each job's wall-clock rounded up to a whole minute, the "
        "rule GitHub bills by. *Used* = unrounded wall-clock. OS multipliers, "
        "larger-runner SKUs and the public-repo exemption are not applied.",
        "",
        "## Totals",
        "",
        _HEADER.format("scope"),
        _row("sample", total, billed),
    ]
    if fleet_size and repos:
        factor = fleet_size / len(repos)
        lines.append(
            f"| fleet estimate (×{factor:.2f}) | {total.runs * factor:,.0f} | "
            f"{total.jobs * factor:,.0f} | {total.wall_minutes * factor:,.0f} | "
            f"{billed * factor:,.0f} | — | {total.ratio:.2f} |"
        )
    lines += [
        "",
        *_table(
            "visibility", tally_by(runs, jobs, _visibility_key(runs)), billed, None
        ),
    ]

    private = {r.repo for r in runs if r.visibility != "public"}
    scopes = [
        ("all scanned repos", headline_shares(runs, jobs)),
        (
            "private repos only (what the billing view charges)",
            headline_shares(
                [r for r in runs if r.repo in private],
                [j for j in jobs if j.repo in private],
            ),
        ),
    ]
    lane_billed = tally_by(runs, jobs, lambda x: x.lane)
    lines += ["", "## Headline shares (of billed minutes)", ""]
    for title, shares in scopes:
        scope_total = shares.pop("total")
        lines += [
            f"**{title}** — {scope_total:,} billed min",
            "",
            "| measure | billed min | share |",
            "|---|---:|---:|",
            *(
                f"| {label} | {value:,} | {_pct(value, scope_total)} |"
                for label, value in shares.items()
            ),
            "",
        ]
    lines += [
        f"## Top {top} workflows",
        "",
        *_table("workflow", tally_by(runs, jobs, lambda x: x.workflow), billed, top),
        "",
        f"## Top {top} jobs",
        "",
        *_table(
            "workflow / job",
            tally_by(None, jobs, lambda x: f"{x.workflow} / {x.job}"),
            billed,
            top,
        ),
        "",
        "## By triggering event",
        "",
        *_table("event", tally_by(runs, jobs, lambda x: x.event), billed, None),
        "",
        "## By actor lane",
        "",
        *_table("lane", lane_billed, billed, None),
        "",
        "## By event × lane",
        "",
        *_table(
            "event / lane",
            tally_by(runs, jobs, lambda x: f"{x.event} / {x.lane}"),
            billed,
            top,
        ),
        "",
        "## Runs per repo per weekday",
        "",
        "| repo | " + " | ".join(WEEKDAYS) + " | total |",
        "|---|" + "---:|" * (len(WEEKDAYS) + 1),
    ]
    counts: dict = defaultdict(lambda: defaultdict(int))
    for r in runs:
        counts[r.repo][r.weekday] += 1
    for repo in repos:
        row = counts[repo]
        lines.append(
            f"| {repo} | "
            + " | ".join(f"{row[d]:,}" for d in WEEKDAYS)
            + f" | {sum(row.values()):,} |"
        )
    return "\n".join(lines) + "\n"


def _visibility_key(runs: list) -> Callable:
    vis = {r.repo: r.visibility for r in runs}
    return lambda x: vis.get(x.repo, "unknown")


# ── entrypoint ──────────────────────────────────────────────────────────────


def main(argv: Optional[list] = None, run: RunFn = _run_gh) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--owner", default="atlanhq")
    parser.add_argument("--since", type=date.fromisoformat, help="first UTC day")
    parser.add_argument("--until", type=date.fromisoformat, help="last UTC day")
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="scan exactly these repos instead of discovering the fleet; repeatable",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="scan a seeded random sample of N fleet repos and extrapolate",
    )
    parser.add_argument("--seed", type=int, default=0, help="sample seed")
    parser.add_argument("--top", type=int, default=15, help="rows in top-N tables")
    parser.add_argument(
        "--out-dir", type=Path, help="write runs.csv/jobs.csv/summary.md"
    )
    parser.add_argument(
        "--from-dir",
        type=Path,
        help="re-render summary.md from an earlier --out-dir; no API calls",
    )
    args = parser.parse_args(argv)

    if args.from_dir:
        meta = json.loads((args.from_dir / "meta.json").read_text())
        runs = read_csv(args.from_dir / "runs.csv", RunRecord)
        jobs = read_csv(args.from_dir / "jobs.csv", JobRecord)
        summary = render_summary(
            runs, jobs, meta["since"], meta["until"], meta.get("fleet_size"), args.top
        )
        (args.from_dir / "summary.md").write_text(summary)
        print(summary)
        return 0

    if not (args.since and args.until and args.out_dir):
        parser.error("--since, --until and --out-dir are required unless --from-dir")

    try:
        fleet_size = None
        if args.repo:
            repos = select_repos([], args.repo, None, args.seed)
        else:
            fleet = discover.discover_fleet(
                args.owner,
                discover.DEFAULT_NAME_PATTERN,
                discover.PRESET_MARKER,
                set(),
                run=lambda a: run(a, None),
            )
            fleet_size = len(fleet)
            repos = select_repos(fleet, [], args.sample, args.seed)
        print(f"Scanning {len(repos)} repos: {', '.join(repos)}", file=sys.stderr)

        runs: list = []
        jobs: list = []
        for repo in repos:
            repo_runs = list_runs(repo, args.since, args.until, run)
            repo_jobs = fetch_jobs(repo_runs, run)
            print(
                f"  {repo}: {len(repo_runs)} runs, {len(repo_jobs)} jobs",
                file=sys.stderr,
            )
            runs += repo_runs
            jobs += repo_jobs
    except (ReportError, discover.DiscoveryError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "runs.csv", runs, RunRecord)
    write_csv(args.out_dir / "jobs.csv", jobs, JobRecord)
    (args.out_dir / "meta.json").write_text(
        json.dumps(
            {
                "since": args.since.isoformat(),
                "until": args.until.isoformat(),
                "fleet_size": fleet_size,
                "repos": repos,
                "seed": args.seed if args.sample is not None else None,
            },
            indent=2,
        )
        + "\n"
    )
    summary = render_summary(
        runs, jobs, args.since.isoformat(), args.until.isoformat(), fleet_size, args.top
    )
    (args.out_dir / "summary.md").write_text(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

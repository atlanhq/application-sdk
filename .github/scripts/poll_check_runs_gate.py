#!/usr/bin/env python3
"""Roll up the async ``Connector E2E run / *`` check runs into a single pass/fail
for the required "Test Gate" (Connector Tests Gate).

The dispatch side (create_check_run.py, invoked from e2e-apps) exits right
after dispatching; the actual result arrives later via a callback from each
connector (complete_check_run.py) that PATCHes its check run directly — no
polling on that side. This script is the one place that still polls, and it
does so cheaply:

* ONE endpoint (``commits/{sha}/check-runs``) covers every connector in a
  single call, instead of one busy-poll per matrix leg.
* Uses conditional requests (``If-None-Match``) — an unchanged poll gets back
  HTTP 304 and does not count against the token's rate limit at all.
* Runs on ``github.token`` (this repo's own bucket), never the shared org PAT
  that the dispatch side still uses to fire the initial workflow_dispatch.

Exits 0 once every name in --name is 'completed' with a passing conclusion,
1 if any concludes non-passing, 1 on timeout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time

PASSING_CONCLUSIONS = {"success", "neutral", "skipped"}


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the HTTP client."""
    return subprocess.run(cmd, **kwargs)


def gh_api_conditional(path: str, *, etag: str | None = None):
    """GET https://api.github.com/{path}, returning (status_code, new_etag, body_json_or_None).

    Uses curl, not `gh api`: `gh api` treats ANY non-2xx response — including
    a 304 Not Modified, which is the entire point of this conditional-request
    pattern — as a command failure, printing its own short diagnostic instead
    of the actual response, so a 304 can't be distinguished from a real error.
    Confirmed in production (run 28949755456): the very first poll that hit an
    unchanged state failed with "gh: HTTP 304" instead of being treated as
    "nothing changed yet" — and since a dispatched e2e run can easily run for
    20+ minutes without its check run changing, an unchanged poll is the
    *common* case here, not an edge case.

    curl without --fail prints the full response (headers + body, via -i) for
    any status code and only exits non-zero on a genuine transport failure
    (timeout, DNS, connection refused) — exactly the raw-HTTP-semantics
    reference used elsewhere in these scripts (see wait_for_pages_publish.py).
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("::error::GH_TOKEN (or GITHUB_TOKEN) must be set")

    cmd = [
        "curl",
        "-sS",
        "-i",
        "--max-time",
        "30",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-H",
        f"Authorization: Bearer {token}",
    ]
    if etag:
        cmd += ["-H", f"If-None-Match: {etag}"]
    cmd.append(f"https://api.github.com/{path}")

    result = run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"::error::curl failed for {path}: {result.stderr}")

    raw = result.stdout.replace("\r\n", "\n")
    if "\n\n" not in raw:
        raise SystemExit(f"::error::unexpected response for {path}: {raw[:300]!r}")
    header_block, _, body = raw.partition("\n\n")
    lines = header_block.splitlines()
    try:
        status_code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise SystemExit(
            f"::error::could not parse HTTP status line: {lines[0] if lines else ''!r}"
        )

    new_etag = etag
    for line in lines[1:]:
        if line.lower().startswith("etag:"):
            new_etag = line.split(":", 1)[1].strip()

    if status_code >= 400:
        raise SystemExit(
            f"::error::GitHub API returned {status_code} for {path}: {body[:500]}"
        )

    body_json = json.loads(body) if status_code == 200 and body.strip() else None
    return status_code, new_etag, body_json


def list_all_check_runs(repo: str, sha: str) -> list[dict]:
    """Full, uncached fetch of every check run on `sha` across all pages.

    Fallback for once a SHA carries more than one page (100) of check
    runs — ETag conditional caching only covers a single page (a page-1
    match doesn't prove page 2+ is unchanged), so once that ceiling is
    crossed we switch to always re-fetching the complete list instead of
    risking a silent miss of a failing check run that lives past page 1.
    Costs more than the conditional path, but only in that (rare, at
    today's matrix sizes) case — and it stays correct rather than failing
    the gate closed for a SHA that's otherwise perfectly resolvable.
    """
    result = run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
            "--jq",
            ".check_runs[] | tojson",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to list check runs for {repo}@{sha}: {result.stderr}"
        )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def wait_for_checks(
    repo: str,
    sha: str,
    expected_names: list[str],
    *,
    interval_seconds: int = 30,
    timeout_seconds: int = 7800,
    sleep=time.sleep,
) -> bool:
    """Poll until every name in `expected_names` has a 'completed' check run
    on `sha`, or the timeout elapses. Returns True iff all conclusions pass."""
    path = f"repos/{repo}/commits/{sha}/check-runs?per_page=100"
    etag: str | None = None
    latest: dict[str, dict] = {}
    # Flips on once a SHA is found to carry more than one page of check
    # runs; from then on every attempt does a full uncached fetch instead
    # of the cheaper conditional single-page GET (see list_all_check_runs).
    paginate_fully = False
    # Ceiling division: a timeout that isn't an exact multiple of the
    # interval (e.g. 31s timeout / 30s interval) must still get its full
    # attempt within budget, not be truncated to fewer attempts than the
    # timeout actually allows.
    max_attempts = max(1, math.ceil(timeout_seconds / interval_seconds))

    for attempt in range(1, max_attempts + 1):
        if paginate_fully:
            check_runs = list_all_check_runs(repo, sha)
            for check_run in check_runs:
                if check_run.get("name") in expected_names:
                    latest[check_run["name"]] = check_run
        else:
            status_code, etag, body = gh_api_conditional(path, etag=etag)
            if status_code == 200 and body is not None:
                check_runs = body.get("check_runs", [])
                total_count = body.get("total_count", len(check_runs))
                if total_count > len(check_runs):
                    # More check runs than a single page holds. ETag caching
                    # only covers page 1 (a match there doesn't prove page
                    # 2+ is unchanged), so from here on always fetch the
                    # full, uncached list rather than risk silently missing
                    # a failing check run past page 1.
                    print(
                        f"::warning::{repo}@{sha} has {total_count} check runs, "
                        f"more than one page ({len(check_runs)}) — switching to "
                        "full (uncached) pagination for the rest of this poll."
                    )
                    paginate_fully = True
                    check_runs = list_all_check_runs(repo, sha)
                for check_run in check_runs:
                    if check_run.get("name") in expected_names:
                        latest[check_run["name"]] = check_run
            elif status_code != 304:
                raise SystemExit(
                    f"::error::unexpected status {status_code} polling {path}"
                )

        missing = [n for n in expected_names if n not in latest]
        pending = [
            n
            for n in expected_names
            if n in latest and latest[n].get("status") != "completed"
        ]
        if not missing and not pending:
            break

        print(
            f"[{attempt}/{max_attempts}] waiting on checks — missing={missing} pending={pending}"
        )
        if attempt < max_attempts:
            sleep(interval_seconds)
    else:
        missing = [n for n in expected_names if n not in latest]
        pending = [
            n
            for n in expected_names
            if n in latest and latest[n].get("status") != "completed"
        ]

    if missing or pending:
        print(
            f"::error::timed out waiting for check runs — missing={missing} pending={pending}"
        )
        return False

    failed = [
        n
        for n in expected_names
        if latest[n].get("conclusion") not in PASSING_CONCLUSIONS
    ]
    if failed:
        print(f"::error::check runs did not pass: {failed}")
        return False

    print(f"All {len(expected_names)} connector check run(s) passed.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument(
        "--sha", required=True, help="Head/merge SHA the check runs are attached to."
    )
    name_group = parser.add_mutually_exclusive_group(required=True)
    name_group.add_argument(
        "--name",
        action="append",
        dest="names",
        help="Expected check run name; repeat once per connector.",
    )
    name_group.add_argument(
        "--names-json",
        help="Expected check run names as a JSON array, e.g. from a matrix built with jq — "
        "avoids the caller having to loop to build repeated --name flags.",
    )
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=7800,
        help="Overall poll budget (default 7800s = 130min, safely above the 120min connector job ceiling).",
    )
    args = parser.parse_args(argv)

    if args.names_json is not None:
        try:
            names = json.loads(args.names_json)
        except json.JSONDecodeError as e:
            raise SystemExit(f"::error::--names-json is not valid JSON: {e}")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise SystemExit("::error::--names-json must be a JSON array of strings")
    else:
        names = args.names

    ok = wait_for_checks(
        args.repo,
        args.sha,
        names,
        interval_seconds=args.interval_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

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

Stopping early when the SHA stops mattering
-------------------------------------------
A check run that never appears is waited out in full — 130 minutes, by design,
because a connector can legitimately take two hours to report. There is one case
where that wait is knowably pointless: this run's SHA is no longer the head of
its PR, and the dispatch guard therefore declined to dispatch for it at all
(``e2e_dispatch_guard.py``, FND-696). Nothing will ever create the checks, so
with ``--pr-number`` this stops as soon as it can see that, and exits 0.

Zero, not one, and the reason is worth stating because "green without a verdict"
is normally the bug: this is a green on a commit that is no longer under review,
where no verdict is required from anyone. A red there would be a false alarm on
an abandoned run — recurring, since a second push in quick succession is routine,
and read by automation that treats a red gate as a real failure. The head
commit's own run is entirely unaffected, and it is the only one whose gate can
satisfy a required check.
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

# This poll runs for up to 130 minutes and its only sign of life is the
# per-attempt "waiting on checks" line. Python block-buffers stdout when it is a
# pipe, which an Actions log always is, so without this the step shows nothing at
# all until it finishes and then prints the whole history at once — a live gate
# and a wedged one look identical (FND-696).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


class Superseded(Exception):
    """The SHA being polled is no longer the head of its PR.

    Raised rather than returned so it cannot be confused with a verdict: there is
    no pass or fail for a commit nobody is going to merge, and the caller has to
    handle it deliberately.
    """

    def __init__(self, head: str) -> None:
        super().__init__(head)
        self.head = head


def pr_head_sha(repo: str, number: int) -> str | None:
    """The head SHA the PR currently points at, or None if it could not be read.

    None is "unknown", never "no head": an unreadable answer must leave the poll
    exactly as it was, because the cost of being wrong here is abandoning the
    wait for checks that were genuinely on their way.

    Hence the SystemExit catch, which is the whole reason this is not a two-line
    function. ``gh_api_conditional`` raises on any >=400 — correct for the poll
    it was written for, where an unreadable check listing is a real failure, and
    exactly wrong here: this read is an optimisation, so a missing
    ``pull-requests: read`` grant would otherwise turn "stop waiting sooner" into
    "kill the gate the first time it looks". A 200 whose body is not JSON raises
    ``json.JSONDecodeError`` from the same parser; swallow that too, matching the
    dispatch-side fail-open, so a proxy HTML page cannot fail the required gate.
    """
    try:
        status_code, _etag, body = gh_api_conditional(f"repos/{repo}/pulls/{number}")
    except (SystemExit, json.JSONDecodeError) as unreadable:
        print(
            f"::warning::could not read pull request #{number} ({unreadable}); "
            "continuing to wait."
        )
        return None
    if status_code != 200 or not isinstance(body, dict):
        print(
            f"::warning::could not read pull request #{number} "
            f"(HTTP {status_code}); continuing to wait."
        )
        return None
    head = body.get("head")
    if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
        return None
    return head["sha"].strip().lower()


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


def check_run_age_key(check_run: dict) -> tuple[str, int]:
    """Sort key that orders same-named check runs oldest-first.

    ``started_at`` is the semantic answer and GitHub renders it as a Zulu
    ISO-8601 stamp, which sorts correctly as a plain string. It is the primary
    key rather than ``id`` because it is the field that means "when this attempt
    began"; ``id`` only breaks ties (same-second creations, or a missing
    ``started_at``), where its monotonicity is what makes it usable at all.
    """
    started_at = check_run.get("started_at")
    check_id = check_run.get("id")
    return (
        started_at if isinstance(started_at, str) else "",
        check_id if isinstance(check_id, int) else 0,
    )


def remember_newest(latest: dict[str, dict], check_run: dict) -> None:
    """Keep only the newest check run per name in ``latest``.

    A SHA can carry SEVERAL check runs under one name: create_check_run.py POSTs
    a new one per dispatch rather than PATCHing the old one, so every re-dispatch
    for the same commit — the supported way to retry a failed connector leg, see
    e2e_dispatch_guard.py's ``is_my_run`` — leaves the previous attempt's check
    behind alongside the new one.

    This used to be a bare ``latest[name] = check_run``, i.e. whichever entry the
    API happened to list last. The listing order of that endpoint is not
    documented, so on a retried SHA the gate could resolve to the SUPERSEDED
    attempt and report the old failure against a leg that had since passed (or
    the reverse). Newest wins, explicitly.
    """
    name = check_run["name"]
    incumbent = latest.get(name)
    if incumbent is None or check_run_age_key(check_run) >= check_run_age_key(
        incumbent
    ):
        latest[name] = check_run


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
    pr_number: int | None = None,
    sleep=time.sleep,
) -> bool:
    """Poll until every name in `expected_names` has a 'completed' check run
    on `sha`, or the timeout elapses. Returns True iff all conclusions pass.

    Raises `Superseded` when `pr_number` is given and `sha` has stopped being
    that PR's head while checks are still missing — nothing will create them.
    """
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
                    remember_newest(latest, check_run)
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
                        remember_newest(latest, check_run)
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

        # Only ever asked about a check that is MISSING, never one that is merely
        # pending: a pending check has a connector run behind it that will report,
        # and abandoning that wait would throw away a real verdict.
        #
        # Attempt 1 is where this normally fires — the checks are created by the
        # dispatch job this one `needs`, so on the head commit they already exist
        # by the first poll. The periodic re-ask afterwards covers the push that
        # lands mid-wait, at one call per five minutes rather than one per poll.
        if missing and pr_number and (attempt == 1 or attempt % 10 == 0):
            head = pr_head_sha(repo, pr_number)
            if head is not None and head != sha.strip().lower():
                raise Superseded(head)

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
        (n, latest[n].get("conclusion"))
        for n in expected_names
        if latest[n].get("conclusion") not in PASSING_CONCLUSIONS
    ]
    if failed:
        # Name the CONCLUSION, not just the check. This used to print bare names,
        # which made a connector whose tests genuinely failed indistinguishable
        # from one that never ran — and the second case has a completely
        # different response (re-run, don't read the diff).
        print(
            "::error::check runs did not pass: "
            + ", ".join(f"{name} ({conclusion})" for name, conclusion in failed)
        )
        cancelled = [name for name, conclusion in failed if conclusion == "cancelled"]
        if cancelled:
            print(
                "::error::cancelled, not failed: "
                + ", ".join(cancelled)
                + " — no test reported a verdict. Usually a concurrency-group "
                "eviction in the connector repo (GitHub keeps only ONE pending "
                "run per group, so a third arrival cancels the queued one before "
                "it gets a runner), or a manual cancel. Re-run rather than "
                "triage the diff. See FND-218."
            )
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
    parser.add_argument(
        "--pr-number",
        default="",
        help="The PR whose head --sha is, on the pull_request path. Given, a "
        "poll for checks that will never exist because this SHA was superseded "
        "stops early instead of waiting out the whole budget. Empty (the "
        "merge_group path) keeps the previous behaviour: a queue entry's SHA is "
        "not any PR's head, so there is nothing for it to fall behind.",
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

    pr_number = args.pr_number.strip()
    try:
        ok = wait_for_checks(
            args.repo,
            args.sha,
            names,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
            pr_number=int(pr_number) if pr_number.isdigit() else None,
        )
    except Superseded as superseded:
        print(
            f"::notice::not waiting for connector checks on {args.sha[:8]}: it is "
            f"no longer the head of PR #{pr_number} ({superseded.head[:8]} is), so "
            "no dispatch was made for it and no check will ever appear. The head "
            "commit's own run carries the verdict."
        )
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

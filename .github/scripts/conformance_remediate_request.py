#!/usr/bin/env python3
"""Build the RemediationRequest for the PR surface and POST it to mothership.

This is the thin adapter half of the conformance-remediation lane. Its entire job
is to turn "a human typed ``/remediate`` on a PR" into one well-formed request, hand
it to the orchestrator, and get out of the way.

Deliberately **fire-and-forget**: it POSTs, receives a ``run_id``, and exits. It does
not stream, poll, or wait. Mothership owns the check run from that point — it has the
GitHub App and it is the thing that learns the outcome — so a GitHub runner is held
for seconds rather than for the sandbox's whole 15–30 minute life. Across a fleet
sweep that difference is hundreds of runner-hours.

**It also does not pre-gate on the published SARIF.** An earlier design read the
newest Conformance run's ``atlan/summary.failing`` and exited "nothing to do" on
zero. That was wrong three ways: the artifact can be stale or absent (many repos
never publish on the ``Conformance`` workflow), ``failing`` counts BLOCK only so a PR
with warnings read as clean, and — decisively — a human explicitly asked. The rover
runs its own pinned detection, which is the only authority; a genuine no-op comes
from that and is reported honestly on the PR.

Loop / conditional logic lives here (a tested script) rather than inlined in
workflow YAML, per docs/standards/ci.md.

Environment:
    MOTHERSHIP_URL   base URL, e.g. https://mothership.atlan.dev
    HARNESS_TOKEN    bearer for the conformance-remediation API
    REPO             owner/name (github.repository)
    PR_NUMBER        the PR carrying the comment
    COMMENT_BODY     the comment text (to parse rule IDs out of)
    COMMENTER        login that asked, for attribution in the report
    GHA_RUN_URL      this workflow run's URL

Optional:
    SUITE_VERSION    pinned conformance version; default DEFAULT_SUITE_VERSION
    DRY_RUN          '1' to print the payload and skip the POST
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

#: Pinned rather than "latest" on purpose: the fleet spans 0.13.0 to 0.20.1, so an
#: unpinned run "fixes" findings that a differently-versioned CI leg will re-raise.
DEFAULT_SUITE_VERSION = "0.20.1"

#: ``/remediate``, optionally followed by rule IDs: ``/remediate L004``,
#: ``/remediate L004,E002``, ``/remediate L004 E002``.
#:
#: The lookahead demands whitespace or end-of-line after the verb, not a mere
#: ``\b``: a word boundary also sits between ``e`` and ``-``, so ``\b`` would make
#: ``/remediate-all`` — or any future ``/remediate-<something>`` command — silently
#: dispatch a remediation run with ``-all`` as its argument list.
COMMAND_RE = re.compile(r"^\s*/remediate(?=\s|$)(?P<args>[^\r\n]*)", re.IGNORECASE)
RULE_ID_RE = re.compile(r"\b([A-Za-z]\d{3})\b")

HEALTH_RETRIES = 3
HEALTH_BACKOFF_SECONDS = 5
POST_TIMEOUT_SECONDS = 60


class NotACommand(Exception):
    """The comment does not invoke /remediate — the job should exit 0, quietly."""


def parse_command(body: str) -> list[str]:
    """Return the requested rule IDs, or ``[]`` for "every failing rule".

    Raises ``NotACommand`` when the comment is not a ``/remediate`` invocation, so a
    caller can distinguish "not for us" (exit 0) from "asked for, but malformed".

    Anchored at the start of the comment: a passing mention of ``/remediate`` inside
    a sentence, or someone quoting an earlier bot comment, must not trigger a run.
    """
    if not body:
        raise NotACommand("empty comment")
    match = COMMAND_RE.match(body)
    if not match:
        raise NotACommand("comment does not start with /remediate")
    return [r.upper() for r in RULE_ID_RE.findall(match.group("args") or "")]


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=60
    ).stdout


def pr_context(
    repo: str, pr: str, runner: Callable[[list[str]], str] = _run
) -> dict[str, Any]:
    """Resolve the PR's head ref/SHA and draft state."""
    raw = runner(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "headRefName,headRefOid,isDraft,state",
        ]
    )
    obj = json.loads(raw)
    return {
        "head_ref": obj.get("headRefName") or "",
        "head_sha": obj.get("headRefOid") or "",
        "is_draft": bool(obj.get("isDraft")),
        "state": obj.get("state") or "",
    }


def build_request(
    *,
    repo: str,
    pr_number: int,
    head_ref: str,
    head_sha: str,
    rules: list[str],
    commenter: str,
    gha_run_url: str,
    suite_version: str,
) -> dict[str, Any]:
    """Assemble the RemediationRequest for the PR surface.

    Every surface-specific choice lives in this object; the orchestrator core reads
    it and has no idea who called. That is the whole segregation — adding a Slack or
    release-gate surface later means writing another builder, not touching the core.
    """
    return {
        "scope": {
            "kind": "pr",
            "repo": repo,
            "pr_number": pr_number,
            "head_ref": head_ref,
            # Pinned so the rover can refuse to act on a branch that moved between
            # the ask and the run — fixing a commit nobody inspected is how you
            # push a change the author never requested.
            "head_sha": head_sha,
            # Empty list = every failing rule on this PR, decided by the rover's
            # own detection rather than by a published artifact.
            "rules": rules,
            "tiers": ["block", "warn"],
        },
        "policy": {
            # Never a second PR to fix the first one: the author already has a PR,
            # so the fix belongs as a commit on its branch.
            "delivery": "push_to_pr_branch",
            # A human is waiting; this must not queue behind a fleet sweep.
            "lane": "interactive",
            "apply_unverifiable": True,
            "suite_version": suite_version,
        },
        "report_to": ["github_check", "github_comment"],
        "origin": "github-pr-comment",
        "origin_id": f"{repo}#{pr_number}",
        # Keyed on the head SHA so re-asking on the same commit is deduped, but a
        # new commit is a genuinely new request.
        "idempotency_key": f"{repo}:{head_sha}:{','.join(rules) or 'all'}",
        "metadata": {
            "commenter": commenter,
            "gha_run_url": gha_run_url,
            "requested_rules": rules,
        },
    }


def check_health(
    base_url: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] | None = None,
) -> bool:
    sleep = sleeper or time.sleep
    for attempt in range(1, HEALTH_RETRIES + 1):
        try:
            with opener(f"{base_url}/health", timeout=10) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if status == 200:
                    print(f"Mothership reachable (attempt {attempt})")
                    return True
        except (urllib.error.URLError, OSError) as e:
            print(f"Mothership unreachable ({e}), retry {attempt}/{HEALTH_RETRIES}")
        else:
            print(f"Mothership non-200, retry {attempt}/{HEALTH_RETRIES}")
        if attempt < HEALTH_RETRIES:
            sleep(HEALTH_BACKOFF_SECONDS)
    return False


def post_request(
    base_url: str,
    token: str,
    payload: dict[str, Any],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, str]:
    """POST the request. Returns ``(http_status, body)``."""
    req = urllib.request.Request(
        f"{base_url}/api/conformance-remediation/remediations",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with opener(req, timeout=POST_TIMEOUT_SECONDS) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        return int(status), resp.read().decode("utf-8", "replace")


def decide_exit(status: int, body: str) -> tuple[int, str]:
    """Map the POST response to ``(exit_code, message)``.

    202 is the expected answer — the orchestrator accepted the request and will
    report the outcome itself. A 200 is accepted too (a synchronous implementation
    would be a valid future change). Anything else is a dispatch failure the
    developer needs to see, because nothing downstream will report it for us.
    """
    if status in (200, 202):
        run_id = ""
        try:
            run_id = str(json.loads(body).get("run_id", ""))
        except (json.JSONDecodeError, AttributeError):
            pass
        suffix = f" run_id={run_id}" if run_id else ""
        return 0, f"Remediation accepted (HTTP {status}){suffix}"
    return 1, f"::error::Remediation dispatch failed: HTTP {status} body={body[:400]}"


def main() -> int:
    repo = os.environ.get("REPO", "")
    pr_raw = os.environ.get("PR_NUMBER", "")
    comment = os.environ.get("COMMENT_BODY", "")

    try:
        rules = parse_command(comment)
    except NotACommand as e:
        # Not our command. Exit 0 — the workflow `if:` should already have filtered
        # this, so treat it as belt-and-braces rather than an error.
        print(f"Not a /remediate invocation ({e}); nothing to do")
        return 0

    if not repo or not pr_raw:
        print("::error::REPO and PR_NUMBER are required")
        return 1
    try:
        pr_number = int(pr_raw)
    except ValueError:
        print(f"::error::PR_NUMBER is not an integer: {pr_raw!r}")
        return 1

    try:
        ctx = pr_context(repo, pr_raw)
    except subprocess.CalledProcessError as e:
        print(f"::error::could not read PR {repo}#{pr_number}: {e.stderr or e}")
        return 1

    if ctx["state"] != "OPEN":
        print(f"::error::PR {repo}#{pr_number} is {ctx['state']}, not OPEN")
        return 1
    if not ctx["head_sha"]:
        print("::error::could not resolve the PR head SHA")
        return 1

    payload = build_request(
        repo=repo,
        pr_number=pr_number,
        head_ref=ctx["head_ref"],
        head_sha=ctx["head_sha"],
        rules=rules,
        commenter=os.environ.get("COMMENTER", ""),
        gha_run_url=os.environ.get("GHA_RUN_URL", ""),
        suite_version=os.environ.get("SUITE_VERSION") or DEFAULT_SUITE_VERSION,
    )

    if os.environ.get("DRY_RUN") == "1":
        print(json.dumps(payload, indent=2))
        return 0

    base_url = os.environ.get("MOTHERSHIP_URL", "").rstrip("/")
    token = os.environ.get("HARNESS_TOKEN", "")
    if not base_url or not token:
        print("::error::MOTHERSHIP_URL and HARNESS_TOKEN are required")
        return 1

    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

    try:
        status, body = post_request(base_url, token, payload)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        print(f"::error::Remediation dispatch transport error: {e}")
        return 1

    code, message = decide_exit(status, body)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Create an in_progress GitHub check run — the dispatch side of the
cross-repo e2e callback pattern.

Used by ``.github/actions/e2e-apps`` (``wait-mode: callback``) right after
dispatching a connector's ``tests.yaml``. The dispatching job creates this
check on *this* repo's PR/merge-queue SHA using ``github.token``, then exits
immediately — no polling. The connector reports back later by PATCHing this
same check run to ``completed`` (see ``complete_check_run.py``), which lives
in the connector's own workflow run and therefore updates the check the
instant the connector's tests finish.

Replaces a busy-poll loop that re-authenticated every request against the
shared org PAT (see docs/standards/ci.md and the e2e-apps action comments)
with a single POST here plus a single PATCH from the connector — the actual
wait no longer costs any API calls at all.

Writes ``check_run_id=<id>`` to $GITHUB_OUTPUT (or stdout if unset) via
``_gha.set_output``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from _gh import GhError, gh_json
from _gha import set_output


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_check_run(
    repo: str,
    sha: str,
    name: str,
    *,
    title: str | None = None,
    summary: str,
    details_url: str | None = None,
) -> int:
    """POST a new in_progress check run. Returns its id."""
    payload = {
        "name": name,
        "head_sha": sha,
        "status": "in_progress",
        "started_at": now_iso(),
        "output": {"title": title or name, "summary": summary},
    }
    if details_url:
        payload["details_url"] = details_url

    try:
        # Not idempotent: a POST that 5xx'd may still have created the run, and
        # a second one would sit in_progress until the watchdog times it out.
        body = gh_json(
            ["api", "--method", "POST", f"repos/{repo}/check-runs", "--input", "-"],
            input=json.dumps(payload),
            idempotent=False,
        )
    except GhError as exc:
        raise SystemExit(
            f"::error::failed to create check run '{name}' on {repo}@{sha}: {exc.stderr}"
        ) from exc
    check_run_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(check_run_id, int):
        raise SystemExit(
            f"::error::creating check run '{name}' on {repo}@{sha} returned no id"
        )
    return check_run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument(
        "--sha", required=True, help="Head SHA to attach the check run to."
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Check run name, e.g. 'Connector E2E run / atlan-mysql-app'.",
    )
    parser.add_argument("--title", default=None)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--details-url", default=None)
    args = parser.parse_args(argv)

    check_run_id = create_check_run(
        args.repo,
        args.sha,
        args.name,
        title=args.title,
        summary=args.summary,
        details_url=args.details_url,
    )

    set_output("check_run_id", str(check_run_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())

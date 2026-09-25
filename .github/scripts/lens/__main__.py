"""lens CLI.

    python -m lens review --repo atlanhq/application-sdk --pr 1234 [--force] [--dry-run]
    python -m lens review --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"

Run from `.github/scripts` against a checkout of the BASE branch (`--root`).
`--dry-run` prints the result as JSON and posts nothing — the way to try lens
on any PR, including a historical one. Exit code is 0 whenever lens ran to a
verdict or a deliberate skip; a non-zero exit means lens itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import approve, report, trace
from .config import load_config, validate
from .event import DISMISS_USAGE, decide
from .github import GitHub
from .llm import Client
from .lock import BUSY_NOTE, WORKFLOW_FILE, older_active_run
from .review import dismiss, render_summary, run, to_json
from .rules import load_rules


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", type=int)
    r.add_argument("--event-name")
    r.add_argument("--event-path")
    r.add_argument("--root", default=".", help="checkout of the base branch (trusted)")
    r.add_argument("--config-dir", default=None, help="default: <root>/.github/lens")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    a = sub.add_parser(
        "approve",
        help="act on the review's approval decision (the workflow's last step)",
    )
    a.add_argument("--repo", required=True)
    a.add_argument("--decision", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "approve":
        return approve.run_step(args.repo, args.decision)

    comment_id = 0
    dismissal = None
    if args.event_name:
        event = json.loads(Path(args.event_path).read_text()) if args.event_path else {}
        d = decide(args.event_name, event, args.repo)
        if not d.run:
            print(f"lens: not running — {d.reason}")
            if d.reason == DISMISS_USAGE and d.comment_id and not args.dry_run:
                gh = GitHub(args.repo)
                gh.comment(d.pr, f"lens: {DISMISS_USAGE}")
                gh.react(d.comment_id, "confused")
            return 0
        dismissal = d if d.dismiss else None
        args.pr, args.force, comment_id = d.pr, args.force or d.force, d.comment_id
        trace.line(
            f"trigger: {args.event_name} on PR #{d.pr}"
            + (" (force)" if d.force else "")
        )
    if not args.pr:
        ap.error("--pr or --event-name is required")
    live = not args.dry_run

    root = Path(args.root).resolve()
    cfg_dir = (
        Path(args.config_dir).resolve()
        if args.config_dir
        else root / ".github" / "lens"
    )
    cfg = load_config(cfg_dir)
    errs = validate(cfg)
    if errs:
        for e in errs:
            print(f"lens: config error: {e}", file=sys.stderr)
        return 2
    rules = load_rules(cfg_dir)
    gh = GitHub(args.repo)

    def react(content: str) -> None:
        if live and comment_id:
            gh.react(comment_id, content)

    # 👀 as soon as the request is accepted, so the author knows lens picked it up.
    react("eyes")

    own_run = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if own_run and live:
        busy = older_active_run(gh.workflow_runs(WORKFLOW_FILE), args.pr, own_run)
        if busy:
            print(
                f"lens: not running — review {busy.get('id')} for #{args.pr} is still in progress"
            )
            gh.comment(
                args.pr,
                BUSY_NOTE.format(url=busy.get("html_url") or f"run {busy.get('id')}"),
            )
            react("confused")
            return 0

    run_url = _run_url()
    if dismissal is not None:
        # No model call and nothing to watch: no progress note.
        res = dismiss(
            gh,
            args.pr,
            dismissal.dismiss,
            dismissal.dismiss_reason,
            actor=dismissal.actor,
            pr_author=dismissal.pr_author,
            post=live,
            run_url=run_url,
        )
        print(f"lens: {res.action} {res.reason}".strip())
        if live:
            approve.write_decision(
                os.environ.get("LENS_APPROVAL_PATH"), args.pr, approve.decision_for(res)
            )
        react("rocket" if res.action == "dismissed" else "confused")
        return 0
    # A "running" note while the review is in progress, with a link to the live log. It is
    # removed once the verdict is posted (the verdict links the run too), and turned into a
    # failure note if lens itself crashes, so it never lingers as a stale "running".
    progress_id = _progress(gh, args.pr, run_url) if live else 0

    def client_factory(ledger):  # noqa: ANN001, ANN202
        client = Client(
            model=cfg.model,
            price=cfg.price,
            ledger=ledger,
            reasoning_effort=cfg.reasoning_effort,
            api=cfg.api,
        )
        # Every model request is logged live, as it completes (counts only, no content).
        client.log = lambda event: print(
            report.call_line(event), file=sys.stderr, flush=True
        )
        return client

    try:
        res = run(
            gh=gh,
            number=args.pr,
            root=root,
            cfg=cfg,
            rules=rules,
            client_factory=client_factory,
            force=args.force,
            post=live,
            run_url=run_url,
        )
    except Exception:
        _best_effort(
            gh.edit_comment,
            progress_id,
            FAILED_NOTE.format(url=run_url or "the Actions log"),
        )
        react("confused")
        raise
    _best_effort(gh.delete_comment, progress_id)
    if live:
        # The approval happens in the workflow's last step, which alone holds the
        # code-owner token (lens/approve.py); this step only decides.
        approve.write_decision(
            os.environ.get("LENS_APPROVAL_PATH"), args.pr, approve.decision_for(res)
        )
    if args.dry_run:
        print(to_json(res))
        if res.action == "reviewed":
            print(
                "\n----- summary comment -----\n" + render_summary(res), file=sys.stderr
            )
    else:
        print(f"lens: {res.action} {res.reason}".strip())
        if res.action == "skipped" and comment_id:
            # Asked, but nothing to do: say why instead of leaving the author guessing.
            gh.comment(args.pr, f"lens: nothing to review — {res.reason}.")
    # Run report: the Actions job summary page and a JSON artifact (see lens.yml).
    try:
        report.write(
            res,
            args.pr,
            os.environ.get("LENS_REPORT_PATH"),
            os.environ.get("GITHUB_STEP_SUMMARY"),
        )
    except OSError as e:
        print(f"lens: could not write the run report: {e}", file=sys.stderr)
    for n in res.notes:
        print(f"lens: note: {n}", file=sys.stderr)
    for r in res.incomplete:
        print(f"lens: incomplete: {r}", file=sys.stderr)
    react(
        "confused"
        if res.failed or res.incomplete
        else ("+1" if res.action == "skipped" else "rocket")
    )
    # A model/transport failure turns the job red. A deliberate budget stop does
    # not: the summary already says the review is incomplete and why.
    return 1 if res.failed else 0


PROGRESS_NOTE = (
    "⏳ **lens is reviewing this PR** — [watch the run live]({url}).\n\n"
    "<sub>This note is removed when the verdict is posted. If it is still here after the run "
    "ends, the run was cancelled or timed out: see the log, then comment `/lens` again.</sub>"
)
FAILED_NOTE = (
    "❌ **lens failed before it could post a verdict** — see {url}. "
    "Comment `/lens` to retry."
)


def _run_url() -> str:
    """This Actions run's page, from the variables GitHub sets in every job."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def _progress(gh: GitHub, pr: int, run_url: str) -> int:
    if not run_url:
        return 0
    try:
        return gh.comment(pr, PROGRESS_NOTE.format(url=run_url))
    except Exception as e:  # noqa: BLE001 - a progress note must never stop the review
        print(f"lens: could not post the progress note: {e}", file=sys.stderr)
        return 0


def _best_effort(fn, comment_id: int, *args) -> None:  # noqa: ANN001
    if not comment_id:
        return
    try:
        fn(comment_id, *args)
    except Exception as e:  # noqa: BLE001 - cleanup must never mask the real outcome
        print(f"lens: progress note cleanup failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

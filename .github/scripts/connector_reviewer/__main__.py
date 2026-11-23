"""connector-reviewer CLI.

    python -m connector_reviewer review --repo atlanhq/atlan-example-app --pr 1234 --root <app checkout> [--force] [--verify-budget] [--dry-run]
    python -m connector_reviewer review --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer finalize --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer sdk-ref --root <app checkout>

`finalize` runs after the review step whatever its outcome (`if: always()`): a
run that crashed, was cancelled or timed out leaves its `pending` status as
`error` instead (connector_reviewer/finalize.py).

Sub-commands of the `/review` comment (`/review force`, `/review verify-budget`,
`/review dismiss`, `/review recheck`) are parsed and documented in connector_reviewer/event.py.

`sdk-ref` reads the app's `uv.lock` and writes the application-sdk ref it pins
(`ref=` and `label=`) to `$GITHUB_OUTPUT`, empty when there is none
(connector_reviewer/sdk_source.py); the workflow checks the SDK source out at
that ref and passes it to `review --sdk-source` (or `CONNECTOR_REVIEWER_SDK_SOURCE`).
The review's tools then read it under `sdk:`. When it was asked for and is not
there, the review runs without it and its summary says so; without the flag or
the variable (a local run) nothing changes.

Run from `.github/scripts` of an application-sdk checkout (where this package
lives), against a checkout of the BASE branch of the connector app under review
(`--root`). The reviewer's own config is the one shipped beside this package
(`.github/connector-reviewer/config.toml` in application-sdk) unless
`--config-dir` says otherwise: an app repo cannot retune its own reviewer.
The fleet principles come from `--principles-dir` (or
`CONNECTOR_REVIEWER_PRINCIPLES_DIR`): in CI, `.github/connector-reviewer/principles/`
of the application-sdk checkout. A review does not start without them. The app's
own principles, if any, are read from `<root>/.github/connector-reviewer/principles/`
of the repo under review (connector_reviewer/principles.py).
`--dry-run` prints the result as JSON and posts nothing — the way to try
connector-reviewer on any PR, including a historical one. Exit code is 0 whenever
connector-reviewer ran to a verdict or a deliberate skip; a non-zero exit means
connector-reviewer itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import report, trace
from .config import load_config, validate
from .event import FORK_REFUSAL, addressed, decide, is_fork
from .finalize import (
    FAILED_NOTE,
    PROGRESS_NOTE,
    finalize,
    pr_number,
    progress_with_head,
)
from .github import GitHub, GitHubError
from .llm import Client, register_secret
from .lock import BUSY_NOTE, WORKFLOW_FILE, older_active_run
from .principles import (
    PrinciplesError,
    PrincipleSet,
    load_principles,
    resolve_fleet_dir,
)
from .review import (
    dismiss,
    incomplete_reasons,
    recheck,
    render_summary,
    run,
    to_json,
    waive,
)
from .sdk_source import resolve_root, step_output
from .tools import SdkSource

# The config that ships with this package: `.github/connector-reviewer/` of the
# application-sdk checkout the package is run from, not of the repo under review.
SHIPPED_CONFIG_DIR = Path(__file__).resolve().parents[2] / "connector-reviewer"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="connector-reviewer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", type=int)
    r.add_argument("--event-name")
    r.add_argument("--event-path")
    r.add_argument(
        "--root",
        default=".",
        help="checkout of the base branch of the repo under review (trusted)",
    )
    r.add_argument(
        "--config-dir",
        default=None,
        help="default: the config shipped beside this package (.github/connector-reviewer)",
    )
    r.add_argument(
        "--principles-dir",
        default=None,
        help="the fleet principles directory: .github/connector-reviewer/principles of "
        "an application-sdk checkout (default: $CONNECTOR_REVIEWER_PRINCIPLES_DIR)",
    )
    r.add_argument("--force", action="store_true")
    r.add_argument(
        "--verify-budget",
        action="store_true",
        help="give the verify call its opt-in output budget (agent.verify_max_tokens_opt_in)",
    )
    r.add_argument("--dry-run", action="store_true")
    r.add_argument(
        "--sdk-source",
        default=None,
        help="checkout of application-sdk at the version the app pins, read under "
        "`sdk:` (default: $CONNECTOR_REVIEWER_SDK_SOURCE; unset: no SDK source)",
    )
    f = sub.add_parser(
        "finalize",
        help="after the review step, whatever its outcome: turn this run's leftover "
        "pending status into error",
    )
    f.add_argument("--repo", required=True)
    f.add_argument("--pr", type=int)
    f.add_argument("--event-name")
    f.add_argument("--event-path")
    s = sub.add_parser(
        "sdk-ref",
        help="write the application-sdk ref the app's uv.lock pins to $GITHUB_OUTPUT",
    )
    s.add_argument("--root", required=True, help="checkout of the app (trusted base)")
    args = ap.parse_args(argv)
    if args.cmd == "finalize":
        return _finalize(args)
    if args.cmd == "sdk-ref":
        return _sdk_ref(args)

    event: dict[str, Any] = {}
    if args.event_name:
        event = json.loads(Path(args.event_path).read_text()) if args.event_path else {}
        if not addressed(args.event_name, event):
            # Most comments on a pull request are not for the reviewer: stop
            # before reading the config or calling GitHub.
            print(
                "connector-reviewer: not running — comment is not addressed to /review"
            )
            return 0
    live = not args.dry_run

    # First thing with a key in reach: an error raised before a client exists is
    # masked too.
    register_secret(os.environ.get("CONNECTOR_REVIEWER_GATEWAY_KEY", ""))
    root = Path(args.root).resolve()
    cfg_dir = Path(args.config_dir).resolve() if args.config_dir else SHIPPED_CONFIG_DIR
    cfg = load_config(cfg_dir)
    errs = validate(cfg)
    if errs:
        for e in errs:
            print(f"connector-reviewer: config error: {e}", file=sys.stderr)
        return 2
    gh = GitHub(args.repo)

    comment_id = 0
    dismissal = None
    rechecking = None
    waiving = None
    if args.event_name:
        # Who may ask is decided from the commenter's live permission on the
        # repository and the trusted Apps in the config (event.py).
        d = decide(
            args.event_name,
            event,
            args.repo,
            trusted_apps=cfg.trusted_apps,
            can_push=gh.can_push,
        )
        if not d.run:
            print(f"connector-reviewer: not running — {d.reason}")
            if d.reply and d.comment_id and live:
                gh.comment(d.pr, f"connector-reviewer: {d.reason}")
                gh.react(d.comment_id, "confused")
            return 0
        dismissal = d if d.dismiss else None
        rechecking = d if d.recheck else None
        waiving = d if d.waive else None
        args.pr, args.force, comment_id = d.pr, args.force or d.force, d.comment_id
        args.verify_budget = args.verify_budget or d.verify_budget
        trace.line(
            f"trigger: {args.event_name} on PR #{d.pr} by @{d.actor or '?'}"
            + (" (force)" if d.force else "")
            + (" (verify-budget)" if d.verify_budget else "")
        )
    if not args.pr:
        ap.error("--pr or --event-name is required")
    # A review judges by the principles, so it does not start without them: a
    # missing fleet directory stops the run here, before any request is made.
    # A dismissal or a re-check reads no principle and needs none.
    principles = PrincipleSet()
    if dismissal is None and rechecking is None and waiving is None:
        try:
            principles = load_principles(resolve_fleet_dir(args.principles_dir), root)
        except PrinciplesError as e:
            print(f"connector-reviewer: config error: {e}", file=sys.stderr)
            return 2

    def react(content: str) -> None:
        if live and comment_id:
            gh.react(comment_id, content)

    # 👀 as soon as the request is accepted, so the author knows connector-reviewer picked it up.
    react("eyes")

    own_run = int(os.environ.get("GITHUB_RUN_ID") or 0)
    if own_run and live:
        busy = older_active_run(
            gh.workflow_runs(WORKFLOW_FILE),
            args.pr,
            own_run,
            jobs_of=gh.run_jobs,
            now=datetime.now(timezone.utc),
        )
        if busy:
            print(
                f"connector-reviewer: not running — review {busy.get('id')} for #{args.pr} is still in progress"
            )
            gh.comment(
                args.pr,
                BUSY_NOTE.format(url=busy.get("html_url") or f"run {busy.get('id')}"),
            )
            react("confused")
            return 0

    def client_factory(ledger):
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

    # A fork's branch is not in this repository: refuse, whoever asked and however.
    if is_fork(gh.pr(args.pr), args.repo):
        print(f"connector-reviewer: not running — {FORK_REFUSAL}")
        if live and comment_id:
            gh.comment(args.pr, f"connector-reviewer: {FORK_REFUSAL}.")
        react("confused")
        return 0

    run_url = _run_url()
    if waiving is not None:
        # No model call and nothing to watch: no progress note.
        res = waive(
            gh,
            args.pr,
            waiving.waive_reason,
            actor=waiving.actor,
            post=live,
            run_url=run_url,
        )
        print(f"connector-reviewer: {res.action} {res.reason}".strip())
        react("rocket" if res.action == "waived" else "confused")
        return 0
    if rechecking is not None:
        # One model call at most, and the verdict follows at once: no progress note.
        res = recheck(
            gh=gh,
            number=args.pr,
            ids=rechecking.recheck,
            reason=rechecking.recheck_reason,
            root=root,
            cfg=cfg,
            client_factory=client_factory,
            actor=rechecking.actor,
            post=live,
            run_url=run_url,
            verify_budget=rechecking.verify_budget,
        )
        print(f"connector-reviewer: {res.action} {res.reason}".strip())
        react("confused" if res.action != "rechecked" or res.incomplete else "rocket")
        return 1 if res.failed else 0
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
        print(f"connector-reviewer: {res.action} {res.reason}".strip())
        react("rocket" if res.action == "dismissed" else "confused")
        return 0
    # The pinned SDK source (FND-5331). Asked for (the flag or the variable,
    # even empty) but not usable: the review runs as without it, and says so.
    sdk: SdkSource | None = None
    sdk_note = ""
    sdk_dir = (
        args.sdk_source
        if args.sdk_source is not None
        else os.environ.get("CONNECTOR_REVIEWER_SDK_SOURCE")
    )
    if sdk_dir is not None:
        sdk, sdk_note = SdkSource.open(sdk_dir, root)
    trace.line(
        f"SDK source: application-sdk {sdk.label} at {sdk.root}"
        if sdk
        else (sdk_note or "SDK source not available: not configured")
    )

    # A "running" note while the review is in progress, with a link to the live log. It is
    # removed once the verdict is posted (the verdict links the run too), and turned into a
    # failure note if connector-reviewer itself crashes, so it never lingers as a stale "running".
    progress_id = _progress(gh, args.pr, run_url) if live else 0

    def record_head(sha: str) -> None:
        # The head this run set `pending` on, on its progress note: if the PR is
        # pushed to while the run is going, finalize still finds that head.
        _best_effort(gh.edit_comment, progress_id, progress_with_head(run_url, sha))

    try:
        res = run(
            gh=gh,
            number=args.pr,
            root=root,
            cfg=cfg,
            principles=principles,
            client_factory=client_factory,
            force=args.force,
            verify_budget=args.verify_budget,
            post=live,
            run_url=run_url,
            on_pending=record_head,
            sdk=sdk,
            sdk_note=sdk_note,
        )
    except Exception:
        # The pending status this run set must not outlive it. The workflow's
        # `finalize` step does the same for a run that is killed outright.
        # Finalize first: it reads the head this run recorded on the progress
        # note, which the failure note below replaces.
        try:
            finalize(gh, args.pr, run_url)
        except Exception as e:  # noqa: BLE001 - must never mask the real failure
            print(f"connector-reviewer: finalize failed: {e}", file=sys.stderr)
        _best_effort(
            gh.edit_comment,
            progress_id,
            FAILED_NOTE.format(url=run_url or "the Actions log"),
        )
        react("confused")
        raise
    _best_effort(gh.delete_comment, progress_id)
    if args.dry_run:
        print(to_json(res))
        if res.action == "reviewed":
            print(
                "\n----- summary comment -----\n" + render_summary(res), file=sys.stderr
            )
    else:
        print(f"connector-reviewer: {res.action} {res.reason}".strip())
        if res.action == "skipped" and comment_id:
            # Asked, but nothing to do: say why instead of leaving the author guessing.
            gh.comment(
                args.pr, f"connector-reviewer: nothing to review — {res.reason}."
            )
    # Run report: the Actions job summary page and a JSON artifact (see connector-reviewer.yaml).
    try:
        report.write(
            res,
            args.pr,
            os.environ.get("CONNECTOR_REVIEWER_REPORT_PATH"),
            os.environ.get("GITHUB_STEP_SUMMARY"),
        )
    except OSError as e:
        print(
            f"connector-reviewer: could not write the run report: {e}", file=sys.stderr
        )
    for n in res.notes:
        print(f"connector-reviewer: note: {n}", file=sys.stderr)
    for r in incomplete_reasons(res):
        print(f"connector-reviewer: incomplete: {r}", file=sys.stderr)
    react(
        "confused"
        if res.failed or incomplete_reasons(res)
        else ("+1" if res.action == "skipped" else "rocket")
    )
    # A model/transport failure turns the job red. A deliberate budget stop does
    # not: the summary already says the review is incomplete and why.
    return 1 if res.failed else 0


def _finalize(args: argparse.Namespace) -> int:
    """`finalize`: no config, no principles, no model; GitHub only. Exit 0 when it
    ran, whether or not it had anything to fix; 1 when GitHub could not be read."""
    number = args.pr or 0
    if not number and args.event_name and args.event_path:
        event = json.loads(Path(args.event_path).read_text())
        number = pr_number(args.event_name, event)
    run_url = _run_url()
    if not number or not run_url:
        print("connector-reviewer: finalize — no pull request or no run to attribute")
        return 0
    try:
        done = finalize(GitHub(args.repo), number, run_url)
    except GitHubError as e:
        print(f"connector-reviewer: finalize failed: {e}", file=sys.stderr)
        return 1
    print(
        f"connector-reviewer: finalize #{number} — "
        + (
            f"status on {', '.join(h[:9] for h in done.errored)} set to error"
            if done.status_set
            else "no pending status of this run"
        )
        + (
            f"; {len(done.notes_fixed)} progress note(s) rewritten"
            if done.notes_fixed
            else ""
        )
    )
    return 0


def _sdk_ref(args: argparse.Namespace) -> int:
    """`sdk-ref`: always exit 0. No ref is an empty output, and the review then
    runs without the SDK source; it is never a reason to fail the job."""
    res = resolve_root(Path(args.root))
    out = step_output(res)
    print(
        f"connector-reviewer: application-sdk pin: {res.pin.label}"
        if res.pin
        else f"connector-reviewer: SDK source not available: {res.reason}"
    )
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(out)
        except OSError as e:
            print(
                f"connector-reviewer: could not write the step output: {e}",
                file=sys.stderr,
            )
    else:
        print(out, end="")
    return 0


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
        print(
            f"connector-reviewer: could not post the progress note: {e}",
            file=sys.stderr,
        )
        return 0


def _best_effort(fn, comment_id: int, *args) -> None:
    if not comment_id:
        return
    try:
        fn(comment_id, *args)
    except Exception as e:  # noqa: BLE001 - cleanup must never mask the real outcome
        print(f"connector-reviewer: progress note cleanup failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

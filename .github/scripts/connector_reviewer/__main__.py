"""connector-reviewer CLI.

    python -m connector_reviewer review --repo atlanhq/atlan-example-app --pr 1234 --root <app checkout> [--force] [--verify-budget] [--dry-run]
    python -m connector_reviewer review --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer finalize --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer sdk-ref --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer sdk-ref --root <app checkout>
    python -m connector_reviewer base-ref --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer gate-ref --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"
    python -m connector_reviewer gate-fact --checkout <head checkout> --venv <new venv dir> --out <fact.json>

`gate-ref` and `gate-fact` run in the workflow's `gate` job, which holds no
secret (FND-5325, connector_reviewer/gate.py). `gate-ref` writes the pull
request's head ref (`ref=refs/pull/<n>/head`, empty when there is none) for a
sparse checkout of the head, read as data; `gate-fact` installs the
conformance version that head's `uv.lock` pins into a fresh venv, compares the
bootstrap-managed gate files with its render, and writes the fact file the
review reads (`--gate-fact`, or `CONNECTOR_REVIEWER_GATE_FACT`). Both always
exit 0: a check that cannot be made is a fact that says why.

`base-ref` reads the pull request through the REST API and writes its base
commit (`base=`, 40 hex, empty when there is none) to `$GITHUB_OUTPUT`; the
workflow checks the app out at that commit for `--root` (FND-5316).

`finalize` runs after the review step whatever its outcome (`if: always()`): a
run that crashed, was cancelled or timed out leaves its `pending` status as
`error` instead (connector_reviewer/finalize.py).

Sub-commands of the `/review` comment (`/review force`, `/review verify-budget`,
`/review dismiss`, `/review recheck`) are parsed and documented in connector_reviewer/event.py.

`sdk-ref` reads the pull request head's `uv.lock` through the REST API (with
`--repo` and a pull request: `--pr`, or the event) and writes the
application-sdk ref it pins (`ref=`, `label=` and the `head=` it read) to
`$GITHUB_OUTPUT`, empty when there is none (connector_reviewer/sdk_source.py);
`--root` alone reads a local checkout's lock instead. The workflow checks the
SDK source out at that ref and passes it to `review --sdk-source` (or
`CONNECTOR_REVIEWER_SDK_SOURCE`). The review reads the lock again at the exact
head it reviews and uses the checkout only when the checkout's commit is the
one that lock pins, so a push between the two steps cannot label one SDK as
another. The review's tools then read it under `sdk:`. When it was asked for and is not there, the review runs without it and
its summary says so; without the flag or the variable (a local run) nothing
changes.

Run from `.github/scripts` of an application-sdk checkout (where this package
lives), against a checkout of the connector app under review at the pull
request's BASE commit (`--root`); the summary says so when the checkout is at
another commit. The reviewer's own config is the one shipped beside this package
(`.github/connector-reviewer/config.toml` in application-sdk) unless
`--config-dir` says otherwise: an app repo cannot retune its own reviewer.
The fleet principles come from `--principles-dir` (or
`CONNECTOR_REVIEWER_PRINCIPLES_DIR`): in CI, `.github/connector-reviewer/principles/`
of the application-sdk checkout. A review does not start without them. The app's
own principles, if any, are read from `.github/connector-reviewer/principles/` of
the repo under review's DEFAULT branch: the checkout `--principles-root` (or
`CONNECTOR_REVIEWER_PRINCIPLES_ROOT`) names, else `--root`
(connector_reviewer/principles.py). In CI that is a checkout of its own, so
neither the pull request nor the branch it targets can rewrite them.
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

from . import gate, report, trace
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
    base_ref_output,
    dismiss,
    incomplete_reasons,
    recheck,
    render_summary,
    run,
    to_json,
    waive,
)
from .sdk_source import Resolution, head_lock, resolve_root, step_output

# The config that ships with this package: `.github/connector-reviewer/` of the
# application-sdk checkout the package is run from, not of the repo under review.
SHIPPED_CONFIG_DIR = Path(__file__).resolve().parents[2] / "connector-reviewer"
# Where the app's own principles are read from when `--principles-root` is not
# given: the default-branch checkout in CI. Unset: `--root`.
PRINCIPLES_ROOT_ENV = "CONNECTOR_REVIEWER_PRINCIPLES_ROOT"


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
        help="checkout of the repo under review at the pull request's base commit "
        "(read as data)",
    )
    r.add_argument(
        "--principles-root",
        default=None,
        help="checkout of the repo under review's default branch, whose "
        ".github/connector-reviewer/principles/ holds its own principles "
        "(default: $CONNECTOR_REVIEWER_PRINCIPLES_ROOT, else --root)",
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
    r.add_argument(
        "--sdk-head",
        default=None,
        help="the pull request head `sdk-ref` chose the SDK checkout's ref from, for "
        "the note when the head moved (default: $CONNECTOR_REVIEWER_SDK_HEAD)",
    )
    r.add_argument(
        "--gate-fact",
        default=None,
        help="the gate job's fact file (connector_reviewer/gate.py) "
        "(default: $CONNECTOR_REVIEWER_GATE_FACT; unset: no gate-file check)",
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
        help="write the application-sdk ref the pull request head's uv.lock pins "
        "to $GITHUB_OUTPUT",
    )
    s.add_argument("--repo", help="owner/name: read the pull request head's uv.lock")
    s.add_argument("--pr", type=int)
    s.add_argument("--event-name")
    s.add_argument("--event-path")
    s.add_argument(
        "--root",
        help="without --repo: a local checkout of the app whose uv.lock is read",
    )
    b = sub.add_parser(
        "base-ref",
        help="write the pull request's base commit to $GITHUB_OUTPUT, for the app "
        "checkout",
    )
    b.add_argument("--repo", required=True)
    b.add_argument("--pr", type=int)
    b.add_argument("--event-name")
    b.add_argument("--event-path")
    g = sub.add_parser(
        "gate-ref",
        help="write the pull request's head ref to $GITHUB_OUTPUT, for the gate job's "
        "checkout",
    )
    g.add_argument("--pr", type=int)
    g.add_argument("--event-name")
    g.add_argument("--event-path")
    gf = sub.add_parser(
        "gate-fact",
        help="compare the head's bootstrap-managed gate files with the render of the "
        "conformance version its uv.lock pins, and write the fact file",
    )
    gf.add_argument(
        "--checkout", required=True, help="the pull request head, read as data"
    )
    gf.add_argument("--venv", required=True, help="a directory for a new venv")
    gf.add_argument(
        "--base-checkout",
        required=True,
        help="the base branch, whose uv.lock pins the conformance version rendered",
    )
    gf.add_argument("--out", required=True, help="where the fact file is written")
    args = ap.parse_args(argv)
    if args.cmd == "gate-ref":
        return _gate_ref(args)
    if args.cmd == "gate-fact":
        return _gate_fact(args)
    if args.cmd == "finalize":
        return _finalize(args)
    if args.cmd == "sdk-ref":
        return _sdk_ref(args)
    if args.cmd == "base-ref":
        return _base_ref(args)

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
        # The app's own principles come from its default branch, never from
        # the base checkout at --root (FND-5316): a pull request, or the branch
        # it targets, must not rewrite the principles it is judged by.
        chosen = args.principles_root or os.environ.get(PRINCIPLES_ROOT_ENV) or ""
        principles_root = Path(chosen).resolve() if chosen else root
        try:
            principles = load_principles(
                resolve_fleet_dir(args.principles_dir), principles_root
            )
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
    # `review.run` opens it against the lock at the very head it reviews, and
    # only when the checkout is at the ref that lock pins (FND-5316).
    sdk_dir = (
        args.sdk_source
        if args.sdk_source is not None
        else os.environ.get("CONNECTOR_REVIEWER_SDK_SOURCE")
    )
    sdk_head = (
        args.sdk_head
        if args.sdk_head is not None
        else os.environ.get("CONNECTOR_REVIEWER_SDK_HEAD", "")
    )
    if sdk_dir is None:
        trace.line("SDK source not available: not configured")
    gate_fact = (
        args.gate_fact if args.gate_fact is not None else os.environ.get(gate.FACT_ENV)
    )
    if gate_fact is None:
        trace.line("gate-file check: not configured")

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
            sdk_dir=sdk_dir,
            sdk_head=sdk_head,
            gate_fact=gate_fact,
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
    head = ""
    if args.repo:
        number = args.pr or 0
        if not number and args.event_name and args.event_path:
            try:
                event = json.loads(Path(args.event_path).read_text())
            except (OSError, ValueError):
                event = {}
            number = pr_number(args.event_name, event) if isinstance(event, dict) else 0
        lock = head_lock(GitHub(args.repo), number)
        res, head = lock.resolution(), lock.head
    elif args.root:
        res = resolve_root(Path(args.root))
    else:
        res = Resolution(
            None, "no pull request (--repo) or app checkout (--root) given"
        )
    out = step_output(res, head)
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


def _base_ref(args: argparse.Namespace) -> int:
    """`base-ref`: always exit 0. No base is an empty output: the app checkout
    then takes its default ref, and the review says it is off the base."""
    number = args.pr or 0
    if not number and args.event_name and args.event_path:
        try:
            event = json.loads(Path(args.event_path).read_text())
        except (OSError, ValueError):
            event = {}
        number = pr_number(args.event_name, event) if isinstance(event, dict) else 0
    out, said = base_ref_output(GitHub(args.repo), number)
    print(f"connector-reviewer: pull request base: {said}")
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


def _read_event(args: argparse.Namespace) -> dict[str, Any]:
    if not (args.event_name and args.event_path):
        return {}
    try:
        event = json.loads(Path(args.event_path).read_text())
    except (OSError, ValueError):
        return {}
    return event if isinstance(event, dict) else {}


def _write_output(out: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(out, end="")
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(out)
    except OSError as e:
        print(
            f"connector-reviewer: could not write the step output: {e}", file=sys.stderr
        )


def _gate_ref(args: argparse.Namespace) -> int:
    """`gate-ref`: always exit 0. No pull request is an empty ref: the
    checkout then fails on its own, and the fact says the head was not checked
    out."""
    out = gate.head_ref_output(args.event_name or "", _read_event(args), args.pr or 0)
    print(f"connector-reviewer: gate-file check reads {out.strip() or 'no ref'}")
    _write_output(out)
    return 0


def _gate_fact(args: argparse.Namespace) -> int:
    """`gate-fact`: always exit 0. A check that cannot be made is a fact with
    `status: unavailable` and the reason, which the review states."""
    fact = gate.make_fact(
        Path(args.checkout), Path(args.venv), Path(args.base_checkout)
    )
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fact), encoding="utf-8")
    except OSError as e:
        print(
            f"connector-reviewer: could not write the gate fact: {e}", file=sys.stderr
        )
    if fact.get("status") == "ok":
        mism = fact.get("mismatches") or {}
        print(
            f"connector-reviewer: gate files at {str(fact.get('head'))[:9]} against "
            f"conformance {fact.get('conformance')}: {len(fact.get('gate_files') or [])} "
            f"checked, {len(mism)} differ from the render"
            + (f" ({', '.join(sorted(mism))})" if mism else "")
        )
    else:
        print(f"connector-reviewer: gate-file check not made: {fact.get('reason')}")
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

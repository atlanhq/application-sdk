"""lens CLI.

    python -m lens review --repo atlanhq/application-sdk --pr 1234 [--force] [--dry-run]
    python -m lens review --repo "$GITHUB_REPOSITORY" --event-name "$GITHUB_EVENT_NAME" --event-path "$GITHUB_EVENT_PATH"

Run from `.github/scripts` against a checkout of the BASE branch (`--root`).
`--dry-run` prints the result as JSON and posts nothing — the mode the
replay bench uses on historical PRs. Exit code is 0 whenever lens ran to a
verdict or a deliberate skip; a non-zero exit means lens itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import load_config, validate
from .event import decide
from .github import GitHub
from .llm import Client
from .lock import BUSY_NOTE, WORKFLOW_FILE, older_active_run
from .review import render_summary, run, to_json
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
    args = ap.parse_args(argv)

    comment_id = 0
    if args.event_name:
        event = json.loads(Path(args.event_path).read_text()) if args.event_path else {}
        d = decide(args.event_name, event, args.repo)
        if not d.run:
            print(f"lens: not running — {d.reason}")
            return 0
        args.pr, args.force, comment_id = d.pr, args.force or d.force, d.comment_id
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

    def client_factory(ledger):  # noqa: ANN001, ANN202
        return Client(
            model=cfg.model,
            price=cfg.price,
            ledger=ledger,
            reasoning_effort=cfg.reasoning_effort,
        )

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
        )
    except Exception:
        react("confused")
        raise
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


if __name__ == "__main__":
    raise SystemExit(main())

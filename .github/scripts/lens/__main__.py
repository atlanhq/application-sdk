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
import sys
from pathlib import Path

from .config import load_config, validate
from .event import decide
from .github import GitHub
from .llm import Client
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

    if args.event_name:
        event = json.loads(Path(args.event_path).read_text()) if args.event_path else {}
        d = decide(args.event_name, event, args.repo)
        if not d.run:
            print(f"lens: not running — {d.reason}")
            return 0
        args.pr, args.force = d.pr, args.force or d.force
    if not args.pr:
        ap.error("--pr or --event-name is required")

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

    def client_factory(ledger):  # noqa: ANN001, ANN202
        return Client(
            model=cfg.model,
            price=cfg.price,
            ledger=ledger,
            reasoning_effort=cfg.reasoning_effort,
        )

    res = run(
        gh=gh,
        number=args.pr,
        root=root,
        cfg=cfg,
        rules=rules,
        client_factory=client_factory,
        force=args.force,
        post=not args.dry_run,
    )
    if args.dry_run:
        print(to_json(res))
        if res.action == "reviewed":
            print(
                "\n----- summary comment -----\n" + render_summary(res), file=sys.stderr
            )
    else:
        print(f"lens: {res.action} {res.reason}".strip())
    for r in res.incomplete:
        print(f"lens: incomplete: {r}", file=sys.stderr)
    # A model/transport failure turns the job red. A deliberate budget stop does
    # not: the summary already says the review is incomplete and why.
    return 1 if res.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``atlan-application-sdk-conformance integration-ledger`` — report IRR.

Two modes:

* **offline** (default) — score from the ledger's declared cadence, marking
  every row "not verified". Useful locally.
* **verified** — pass ``--repo-root`` to re-derive the denominator from the
  connector checkouts, and ``--github`` to re-derive cadence from the GitHub
  Actions API. This is the mode CI runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from conformance.ledger.compute import LedgerDriftError, evaluate
from conformance.ledger.schema import DEFAULT_LEDGER_PATH, Ledger

_ORG = "atlanhq"


@dataclass(frozen=True)
class _Run:
    trigger: str
    conclusion: str
    age_days: int


class GitHubActionsClient:
    """Thin ``gh``-backed client. Kept out of :mod:`.compute` so scoring stays
    pure and offline-testable."""

    def __init__(self, org: str = _ORG) -> None:
        self._org = org

    def latest_run(self, repo, workflow_file, job=None):  # noqa: ARG002
        import subprocess

        wf = Path(workflow_file).name
        try:
            out = subprocess.run(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    f"{self._org}/{repo}",
                    "--workflow",
                    wf,
                    "--limit",
                    "1",
                    "--json",
                    "event,conclusion,createdAt",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            return None

        runs = json.loads(out or "[]")
        if not runs:
            return None

        from datetime import datetime, timezone

        r = runs[0]
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - created).days
        return _Run(
            trigger=r.get("event", ""),
            conclusion=r.get("conclusion") or "",
            age_days=age,
        )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="… integration-ledger")
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="directory holding the connector checkouts; enables denominator "
        "verification (a drifted ledger becomes an error)",
    )
    ap.add_argument(
        "--github",
        action="store_true",
        help="verify cadence against the GitHub Actions API via `gh`",
    )
    ap.add_argument("--connector", action="append", dest="only")
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit 1 if fleet IRR is below this percentage",
    )
    args = ap.parse_args(argv)

    ledger = Ledger.load(args.ledger)
    actions = GitHubActionsClient() if args.github else None

    try:
        fleet = evaluate(
            ledger, repo_root=args.repo_root, actions=actions, only=args.only
        )
    except LedgerDriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        json.dump(
            {
                "irr": round(fleet.irr * 100, 1),
                "covered": fleet.covered,
                "total": fleet.total,
                "verified": {
                    "denominator": args.repo_root is not None,
                    "cadence": args.github,
                },
                "connectors": [
                    {
                        "name": c.name,
                        "irr": round(c.irr * 100, 1),
                        "covered": c.covered,
                        "total": c.total,
                        "workflows": [
                            {"id": w.id, "covered": w.covered, "reason": w.reason}
                            for w in c.workflows
                        ],
                    }
                    for c in fleet.connectors
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        for c in fleet.connectors:
            print(f"{c.name:26} {c.covered}/{c.total}")
            for w in c.workflows:
                mark = "ok  " if w.covered else "MISS"
                print(f"  {mark} {w.id:32} {w.reason}")
        print(f"\n{'FLEET':26} {fleet.covered}/{fleet.total} = {fleet.irr * 100:.0f}%")
        if not args.repo_root or not args.github:
            print(
                "\nnote: run with --repo-root and --github for a verified score.",
                file=sys.stderr,
            )

    if args.fail_under is not None and fleet.irr * 100 < args.fail_under:
        print(
            f"error: fleet IRR {fleet.irr * 100:.1f} is below "
            f"--fail-under {args.fail_under}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

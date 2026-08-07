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

from conformance.ledger.report import evaluate_repo

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
    ap.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="connector checkout to score (default: cwd)",
    )
    ap.add_argument(
        "--connector",
        default=None,
        help="repo name, e.g. atlan-bigquery-app (default: the directory name)",
    )
    ap.add_argument(
        "--ci-workflow",
        default=None,
        help="workflow file whose cadence backs this repo's integration lanes",
    )
    ap.add_argument(
        "--github",
        action="store_true",
        help="verify cadence against the GitHub Actions API via `gh`",
    )
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit 1 if IRR is below this percentage",
    )
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    connector = args.connector or repo.name
    actions = GitHubActionsClient() if args.github else None

    report = evaluate_repo(repo, connector, actions, args.ci_workflow)

    if args.as_json:
        json.dump(
            {
                "connector": report.name,
                "irr": round(report.irr * 100, 1),
                "covered": report.covered,
                "total": report.total,
                "cadence_verified": args.github,
                "workflows": [
                    {
                        "id": w.id,
                        "declared_at": w.declared_at,
                        "covered": w.covered,
                        "depth": w.depth.value,
                        "reason": w.reason,
                    }
                    for w in report.workflows
                ],
                "orphan_declarations": report.orphan_declarations,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        print(f"{report.name}  {report.covered}/{report.total}")
        for w in report.workflows:
            mark = "ok  " if w.covered else "MISS"
            print(f"  {mark} {w.id:32} [{w.depth.value}] {w.reason}")
        for orphan in report.orphan_declarations:
            print(
                f"  WARN a test declares entrypoint {orphan!r}, which the app "
                f"does not define"
            )
        print(f"\nIRR {report.irr * 100:.0f}%")
        if not args.github:
            print(
                "\nnote: run with --github to verify cadence.",
                file=sys.stderr,
            )

    if args.fail_under is not None and report.irr * 100 < args.fail_under:
        print(
            f"error: IRR {report.irr * 100:.1f} is below --fail-under "
            f"{args.fail_under}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

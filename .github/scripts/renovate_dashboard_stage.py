#!/usr/bin/env python3
"""Stage the Renovate dashboard's S3 payload as one directory tree (FND-909).

The deploy step used to walk the scanner's output repo by repo, issuing an
`aws s3 cp` per file and, for history, a download and an upload per file. At 548
repos that is ~1,644 sequential CLI invocations, each paying process startup on
top of the request. Measured 2026-08-29: 39m18s, which overran the job's
45-minute ceiling and killed the run *after* a successful scan — so the dashboard
still did not publish and the alarm step never ran.

This script does the part that never needed the network: it merges the run's new
history into the existing history already downloaded, and lays everything out in
a single directory that mirrors the S3 prefix exactly. The workflow then moves it
with two `aws s3 sync` calls (one down, one up), which parallelise internally.

Layout produced under --stage-dir, mirroring s3://<bucket>/renovate-dashboard/:

    repos/<slug>.json       every per-repo summary the scanner emitted
    history/<slug>.jsonl    existing history + this run's rows, deduplicated
    fleet.json              full-fleet runs only

Single-repo mode writes no fleet.json and no fleet history: the scanner only has
data for one repo, so publishing either would overwrite the real fleet aggregate
with a partial view. That decision lives here rather than in the workflow because
`docs/standards/ci.md` keeps branching logic out of YAML, where it cannot be
regression-tested.

Usage:
    renovate_dashboard_stage.py --output-dir /tmp/renovate-output \\
        --existing-history-dir /tmp/existing-history \\
        --stage-dir /tmp/renovate-stage [--single-repo owner/name]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional

# The scanner names fleet-wide history differently from a repo's, and it must
# never be treated as a repo slug.
FLEET_SLUG = "fleet"
HISTORY_PREFIX = "history_"
HISTORY_SUFFIX = ".jsonl"


def merge_history(existing: Optional[Path], new: Path) -> str:
    """Existing rows plus this run's, deduplicated, in stable sorted order.

    Mirrors the `cat ... | sort -u` the shell loop did. Sorting is what makes a
    same-day rerun idempotent: the scanner writes one row per repo per run, and
    an identical row must not accumulate.
    """
    rows: set[str] = set()
    for path in (existing, new):
        if path is None or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                rows.add(line)
    return "".join(f"{row}\n" for row in sorted(rows))


def stage(
    output_dir: Path,
    existing_history_dir: Path,
    stage_dir: Path,
    single_repo: str = "",
) -> dict[str, int]:
    """Build the upload tree. Returns counts for the workflow log."""
    repos_out = stage_dir / "repos"
    history_out = stage_dir / "history"
    repos_out.mkdir(parents=True, exist_ok=True)
    history_out.mkdir(parents=True, exist_ok=True)

    repo_files = sorted((output_dir / "repos").glob("*.json"))
    for path in repo_files:
        shutil.copyfile(path, repos_out / path.name)

    merged = 0
    for path in sorted(output_dir.glob(f"{HISTORY_PREFIX}*{HISTORY_SUFFIX}")):
        slug = path.name[len(HISTORY_PREFIX) : -len(HISTORY_SUFFIX)]
        if slug == FLEET_SLUG:
            continue  # handled below, and only for full-fleet runs
        existing = existing_history_dir / f"{slug}{HISTORY_SUFFIX}"
        (history_out / f"{slug}{HISTORY_SUFFIX}").write_text(
            merge_history(existing, path), encoding="utf-8"
        )
        merged += 1

    fleet_written = 0
    if not single_repo:
        fleet_json = output_dir / "fleet.json"
        if not fleet_json.is_file():
            # Fail closed, matching publish_fleet_dashboard.py. Staging the
            # per-repo files without the aggregate would publish a dashboard
            # whose fleet numbers silently belong to the previous run. The
            # `aws s3 cp` this replaced got that for free under `set -euo
            # pipefail`; skipping the file quietly would be a regression.
            raise RuntimeError(
                f"{fleet_json} is missing on a full-fleet run — refusing to "
                "stage per-repo data without the aggregate the dashboard reads"
            )
        shutil.copyfile(fleet_json, stage_dir / "fleet.json")
        fleet_written = 1
        fleet_history = output_dir / f"{HISTORY_PREFIX}{FLEET_SLUG}{HISTORY_SUFFIX}"
        if fleet_history.is_file():
            existing = existing_history_dir / f"{FLEET_SLUG}{HISTORY_SUFFIX}"
            (history_out / f"{FLEET_SLUG}{HISTORY_SUFFIX}").write_text(
                merge_history(existing, fleet_history), encoding="utf-8"
            )

    return {
        "repos": len(repo_files),
        "histories": merged,
        "fleet_json": fleet_written,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--existing-history-dir", required=True, type=Path)
    parser.add_argument("--stage-dir", required=True, type=Path)
    parser.add_argument(
        "--single-repo",
        default="",
        help="non-empty for a single-repo scan: suppresses fleet aggregates",
    )
    args = parser.parse_args(argv)

    if not args.output_dir.is_dir():
        print(f"scanner output not found: {args.output_dir}", file=sys.stderr)
        return 1

    try:
        counts = stage(
            args.output_dir,
            args.existing_history_dir,
            args.stage_dir,
            args.single_repo,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"staged {counts['repos']} repo files, {counts['histories']} histories, "
        f"fleet.json={'yes' if counts['fleet_json'] else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

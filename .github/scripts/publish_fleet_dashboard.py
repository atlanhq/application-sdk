#!/usr/bin/env python3
"""Publish a fleet-scan output directory to the Kryptonite dashboard store.

The upload half of the fleet-dashboard pattern: per-repo documents, a manifest
of every repo present in the bucket, append-only per-repo history, and — on
full-fleet runs only — the fleet aggregate and its history.

The manifest is rebuilt by LISTING the bucket, which has no deletion path, so a
repo that left the fleet stayed enumerated on every dashboard forever. On a
full-fleet run the scan itself is authoritative about membership, and stored
objects it did not produce are omitted from the manifest (never deleted — see
``refresh_manifest``). FND-960.

Extracted from an inlined ``run:`` block per docs/standards/ci.md. The shell
version carried a loop, an if, and a download → merge → re-upload sequence whose
failure mode is silent: without a ``touch`` of the not-yet-existing history file,
``cat`` exits 1, ``pipefail`` kills the step *after* the per-repo document has
already uploaded, and the repo gets a summary in S3 but never a history line.
That is exactly the class of branch YAML cannot regression-test, so it lives
here and is covered in tests/test_publish_fleet_dashboard.py.

Single-repo mode deliberately skips ``fleet.json`` and the fleet history: a scan
of one repo has no fleet aggregate to publish, and writing one would replace the
real fleet view with a one-repo view that reads as a catastrophic regression.

Environment:
    AWS credentials must already be configured (the caller assumes the
    kryptonite-store role via OIDC before invoking this).

Usage:
    publish_fleet_dashboard.py --dir /tmp/gate-enforcement \\
        --prefix gate-enforcement-dashboard
    publish_fleet_dashboard.py --dir /tmp/gate-enforcement \\
        --prefix gate-enforcement-dashboard --single-repo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

BUCKET = "s3://kryptonite-store"

# (args) -> (returncode, stdout, stderr). Not raising on failure: a missing
# history object is a routine first-publish, and the caller distinguishes that
# from a real error by the download's stderr — which is why stderr is carried
# here rather than only logged.
RunFn = Callable[[list], tuple]

FLEET_SLUG = "fleet"

# Ceiling on how much of a stored manifest one full-fleet publish may drop.
#
# Orphan detection is inference from absence: an object the scan did not produce
# is assumed to belong to a repo that left the fleet. That is only as good as the
# scan — a partial one (rate limit, a failed discovery page, a crash midway)
# makes most of the fleet look departed. Above this fraction we keep everything
# and say so, because a manifest that still lists a dead repo is a cosmetic
# problem while a manifest missing 80% of the fleet reads as a catastrophic
# regression on every dashboard at once.
MAX_ORPHAN_FRACTION = 0.2


def _run_aws(args: list) -> tuple:
    result = subprocess.run(["aws", *args], capture_output=True, text=True)
    if result.returncode != 0 and result.stderr:
        print(
            f"::debug::aws {' '.join(args[:2])}: {result.stderr.strip()}",
            file=sys.stderr,
        )
    return result.returncode, result.stdout, result.stderr


def _key(prefix: str, *parts: str) -> str:
    return f"{BUCKET}/{prefix}/" + "/".join(parts)


def upload(local: Path, prefix: str, *parts: str, run: RunFn = _run_aws) -> None:
    code, _, _ = run(["s3", "cp", str(local), _key(prefix, *parts)])
    if code != 0:
        raise RuntimeError(f"failed to upload {local} to {_key(prefix, *parts)}")


def _is_not_found(stderr: str) -> bool:
    """True only when the aws CLI reports a genuine missing-object 404.

    Matched on the ``(404)`` + ``HeadObject`` signature rather than the bare
    ``does not exist`` substring, so a contrived non-404 error that happens to
    embed that phrase is not misclassified as a first publish.
    """
    return "(404)" in stderr and "HeadObject" in stderr


def _dedupe_by_date(lines: list) -> list:
    """One entry per ``date``, last writer winning; unparseable lines preserved.

    Deduplicating on the whole line — the original ``sort -u`` — only collapses a
    rerun whose output is byte-identical. Anything that changes the line for a
    date already written keeps *both*: a repo that gets gated at noon, a value
    that moves between two runs on one day, or a schema change to the entry
    shape. The series then carries two contradicting points on the same x, and
    the newer one does not obviously win.

    Since these files are daily snapshots, the date is the key. Callers pass
    stored lines first and the fresh line last, so the fresh one wins.

    Lines that are not JSON objects with a ``date`` are not part of the series;
    they are kept verbatim ahead of it rather than dropped, because deleting
    data we cannot interpret is worse than carrying it.
    """
    by_date: dict = {}
    preserved: list = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            preserved.append(line)
            continue
        date = entry.get("date") if isinstance(entry, dict) else None
        if not isinstance(date, str):
            preserved.append(line)
            continue
        by_date[date] = line
    return preserved + [by_date[d] for d in sorted(by_date)]


def merge_history(
    local: Path, prefix: str, slug: str, tmp_dir: Path, run: RunFn = _run_aws
) -> None:
    """Merge ``local``'s lines into the stored history for ``slug``.

    Download → merge one-entry-per-date → re-upload. See ``_dedupe_by_date`` for
    why the date rather than the whole line is the key.
    """
    existing = tmp_dir / f"existing_{slug}.jsonl"
    key = _key(prefix, "history", f"{slug}.jsonl")
    # A missing object is the first publish for this repo — proceed with an
    # empty stand-in. Any *other* download failure (AccessDenied, throttling,
    # timeout) must abort BEFORE the upload: treating it as "no history yet"
    # would merge only today's line and re-upload it over the stored history,
    # silently truncating the trend line the dashboard depends on.
    code, _, stderr = run(["s3", "cp", key, str(existing)])
    if code != 0 and not _is_not_found(stderr):
        raise RuntimeError(f"failed to download {key}: {stderr.strip() or 'no stderr'}")
    lines = existing.read_text().splitlines() if existing.exists() else []
    lines.extend(local.read_text().splitlines())

    merged = tmp_dir / f"merged_{slug}.jsonl"
    merged.write_text("\n".join(_dedupe_by_date(lines)) + "\n")
    upload(merged, prefix, "history", f"{slug}.jsonl", run=run)


def orphans(stored: list, scanned: set) -> list:
    """Stored objects this full-fleet scan did not produce, sorted.

    These are repos that have left the fleet. The scanners already exclude
    archived repos (``gh repo list --no-archived``), so an archived repo simply
    stops being scanned — and because the manifest is rebuilt by LISTING the
    bucket, which has no deletion path, its object kept it on every dashboard
    forever, padding the rollout-coverage denominator (FND-960).
    """
    return sorted(name for name in stored if name not in scanned)


def refresh_manifest(
    prefix: str,
    tmp_dir: Path,
    run: RunFn = _run_aws,
    *,
    scanned: Optional[set] = None,
) -> list:
    """Rewrite ``repos.json`` from what is actually in the bucket.

    Listing the bucket rather than the local scan output on purpose: in
    single-repo mode the local directory holds one file, and a manifest built
    from it would hide every other repo from the dashboard.

    On a full-fleet run the caller passes ``scanned`` — the object names this
    scan produced — which IS authoritative about fleet membership. Stored
    objects absent from it are omitted from the manifest, so a departed repo
    stops being enumerated by every consumer.

    Omitted, not deleted. Excluding is idempotent (recomputed from ``scanned``
    on every run) and costs nothing if the judgement is wrong, whereas an
    automated delete of dashboard history cannot be undone. Reclaiming the
    objects themselves stays a deliberate, human-triggered act —
    ``prune-dashboard-repo.yaml``.
    """
    code, stdout, _ = run(["s3", "ls", _key(prefix, "repos", "")])
    if code != 0:
        raise RuntimeError(f"failed to list {_key(prefix, 'repos', '')}")
    names = sorted(
        line.split()[-1]
        for line in stdout.splitlines()
        if line.strip().endswith(".json")
    )

    dropped: list = []
    if scanned is not None:
        candidates = orphans(names, scanned)
        cap = max(1, int(len(names) * MAX_ORPHAN_FRACTION))
        if len(candidates) > cap:
            print(
                f"::warning::{len(candidates)} of {len(names)} stored repos are absent "
                f"from this scan (cap {cap}) — keeping all of them; the scan looks "
                "partial, not the fleet shrunk",
                file=sys.stderr,
            )
        elif candidates:
            dropped = candidates
            names = [n for n in names if n not in set(dropped)]
            for name in dropped:
                print(
                    f"::notice::omitting {name} from {prefix}/repos.json — no longer "
                    "in the fleet scan",
                    file=sys.stderr,
                )

    manifest = tmp_dir / "repos.json"
    manifest.write_text(json.dumps(names))
    upload(manifest, prefix, "repos.json", run=run)
    return names


def publish(
    scan_dir: Path,
    prefix: str,
    single_repo: bool,
    tmp_dir: Path,
    run: RunFn = _run_aws,
) -> dict:
    """Upload one scan directory. Returns a counts summary for the run log."""
    repo_docs = sorted((scan_dir / "repos").glob("*.json"))
    if not repo_docs:
        raise RuntimeError(f"no per-repo documents in {scan_dir / 'repos'}")

    for doc in repo_docs:
        upload(doc, prefix, "repos", doc.name, run=run)

    # A full-fleet scan is authoritative about membership, so it can retire
    # departed repos from the manifest. A single-repo scan is not — passing its
    # one document would omit the entire rest of the fleet.
    manifest = refresh_manifest(
        prefix,
        tmp_dir,
        run=run,
        scanned=None if single_repo else {doc.name for doc in repo_docs},
    )

    histories = 0
    for history in sorted(scan_dir.glob("history_*.jsonl")):
        slug = history.stem[len("history_") :]
        if slug == FLEET_SLUG:
            continue
        merge_history(history, prefix, slug, tmp_dir, run=run)
        histories += 1

    fleet_published = False
    if not single_repo:
        fleet = scan_dir / "fleet.json"
        if not fleet.exists():
            raise RuntimeError(
                f"{fleet} is missing on a full-fleet run — refusing to publish "
                "per-repo data without the aggregate the dashboard reads"
            )
        upload(fleet, prefix, "fleet.json", run=run)
        fleet_history = scan_dir / f"history_{FLEET_SLUG}.jsonl"
        if fleet_history.exists():
            merge_history(fleet_history, prefix, FLEET_SLUG, tmp_dir, run=run)
        fleet_published = True

    return {
        "repoDocs": len(repo_docs),
        "manifestEntries": len(manifest),
        "historiesMerged": histories,
        "fleetPublished": fleet_published,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", required=True, type=Path, help="scan output directory")
    parser.add_argument(
        "--prefix", required=True, help="S3 key prefix, e.g. gate-enforcement-dashboard"
    )
    parser.add_argument(
        "--single-repo",
        action="store_true",
        help="scan covered one repo: skip fleet.json and the fleet history",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        summary = publish(args.dir, args.prefix, args.single_repo, Path(tmp))

    print(
        f"Published {summary['repoDocs']} repo docs, {summary['historiesMerged']} "
        f"histories, manifest of {summary['manifestEntries']} "
        f"(fleet aggregate: {'yes' if summary['fleetPublished'] else 'skipped'}) "
        f"to {BUCKET}/{args.prefix}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

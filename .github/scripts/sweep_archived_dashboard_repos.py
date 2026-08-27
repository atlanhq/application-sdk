#!/usr/bin/env python3
"""Sweep archived repos out of one or more Kryptonite dashboard prefixes.

Every dashboard's ``repos.json`` is rebuilt by LISTING the bucket, so a repo
that leaves the fleet keeps being enumerated forever: archiving stops it being
updated, and nothing removes its object. ``publish_fleet_dashboard.py`` now
omits departed repos from the manifest on full-fleet runs, but the per-repo
publishers (security / conformance / test-readiness, driven from each app repo's
own ``update-dashboard.yml``) have no full-fleet view and cannot. This is the
deliberate, human-triggered cleanup for those — FND-960.

Deletion is limited to repos GitHub definitively reports as ``archived: true``:

* A repo that 404s (deleted, renamed, or invisible to this token) is REPORTED,
  never swept. The three causes need different follow-ups and a token that lost
  scope would otherwise read as "the whole fleet was deleted".
* An unreadable answer is likewise reported, never acted on.

A delete the bucket refuses (AccessDenied, throttling) leaves the slug in
``repos.json``: the manifest tracks what is actually stored, so a row may only
disappear once its objects have.

``--max-fraction`` caps how much of a prefix one invocation may remove, because
a bad token makes everything look archived and a manifest missing most of the
fleet reads as a catastrophic regression on every panel at once.

Usage:
    sweep_archived_dashboard_repos.py --prefixes conformance-dashboard --dry-run
    sweep_archived_dashboard_repos.py --prefixes security-dashboard,renovate-dashboard

Environment:
    GH_TOKEN  bearer token for `gh` (org read)
    AWS credentials must already be configured by the caller.
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

# (args) -> (returncode, stdout, stderr). Same seam shape as
# publish_fleet_dashboard.py so both are testable against a fake bucket.
RunFn = Callable[[list], tuple]
GhFn = Callable[[str], tuple]

DEFAULT_MAX_FRACTION = 0.2


def _run_aws(args: list) -> tuple:
    result = subprocess.run(["aws", *args], capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _run_gh(repo: str) -> tuple:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}", "-q", ".archived"],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def slug_to_repo(slug: str) -> Optional[str]:
    """``atlanhq_atlan-mysql-app`` -> ``atlanhq/atlan-mysql-app``.

    Returns None for anything that does not carry an ``<owner>_<name>`` shape,
    so an unexpected object in the prefix is skipped rather than turned into a
    bogus repo name we then ask GitHub about.
    """
    owner, sep, name = slug.partition("_")
    if not sep or not owner or not name:
        return None
    return f"{owner}/{name}"


def list_slugs(prefix: str, run: RunFn = _run_aws) -> list:
    """Slugs (object basenames, ``.json`` stripped) stored under *prefix*."""
    code, stdout, stderr = run(["s3", "ls", f"{BUCKET}/{prefix}/repos/"])
    if code != 0:
        raise RuntimeError(f"failed to list {prefix}/repos/: {stderr.strip()}")
    return sorted(
        line.split()[-1][: -len(".json")]
        for line in stdout.splitlines()
        if line.strip().endswith(".json")
    )


def archived_state(repo: str, gh: GhFn = _run_gh) -> Optional[bool]:
    """True/False when GitHub answers, None when it cannot be determined.

    None is the important case: it must never be treated as archived. A 404 or a
    403 says something about our token or the repo's visibility, not about
    whether the fleet still contains it.
    """
    code, stdout, _ = gh(repo)
    if code != 0:
        return None
    answer = stdout.strip().lower()
    if answer in ("true", "false"):
        return answer == "true"
    return None


def rebuild_manifest(
    prefix: str, slugs: list, tmp_dir: Path, run: RunFn = _run_aws
) -> None:
    """Write ``repos.json`` for *prefix* from *slugs* (object names restored)."""
    manifest = tmp_dir / "repos.json"
    manifest.write_text(json.dumps(sorted(f"{s}.json" for s in slugs)))
    code, _, stderr = run(["s3", "cp", str(manifest), f"{BUCKET}/{prefix}/repos.json"])
    if code != 0:
        raise RuntimeError(f"failed to upload {prefix}/repos.json: {stderr.strip()}")


def sweep(
    prefix: str,
    tmp_dir: Path,
    *,
    run: RunFn = _run_aws,
    gh: GhFn = _run_gh,
    dry_run: bool = False,
    max_fraction: float = DEFAULT_MAX_FRACTION,
) -> dict:
    """Remove archived repos' objects from *prefix* and rebuild its manifest."""
    slugs = list_slugs(prefix, run=run)
    archived: list = []
    unknown: list = []
    for slug in slugs:
        repo = slug_to_repo(slug)
        if repo is None:
            unknown.append(slug)
            continue
        state = archived_state(repo, gh)
        if state is None:
            unknown.append(slug)
        elif state:
            archived.append(slug)

    cap = max(1, int(len(slugs) * max_fraction))
    if len(archived) > cap:
        print(
            f"::warning::{prefix}: {len(archived)} of {len(slugs)} repos read as "
            f"archived (cap {cap}) — sweeping none; check the token before retrying",
            file=sys.stderr,
        )
        return {
            "prefix": prefix,
            "stored": len(slugs),
            "swept": [],
            "refused": archived,
            "unknown": unknown,
        }

    for slug in unknown:
        print(
            f"::notice::{prefix}: {slug} — state unreadable, left in place",
            file=sys.stderr,
        )

    if not archived:
        print(f"{prefix}: nothing archived among {len(slugs)} repos", file=sys.stderr)
        return {
            "prefix": prefix,
            "stored": len(slugs),
            "swept": [],
            "refused": [],
            "unknown": unknown,
        }

    swept: list = []
    undeleted: list = []
    for slug in archived:
        print(
            f"{prefix}: {'would sweep' if dry_run else 'sweeping'} {slug}",
            file=sys.stderr,
        )
        if dry_run:
            swept.append(slug)
            continue
        # A missing object is fine — `aws s3 rm` exits 0 for a key that is
        # already gone, and the point is that it is gone afterwards. A non-zero
        # code is therefore a real refusal (AccessDenied, throttling): the
        # object survives, so its manifest row has to survive with it rather
        # than the slug vanishing from repos.json while its data stays.
        codes = [
            run(["s3", "rm", f"{BUCKET}/{prefix}/repos/{slug}.json"])[0],
            run(["s3", "rm", f"{BUCKET}/{prefix}/history/{slug}.jsonl"])[0],
        ]
        if any(codes):
            print(
                f"::warning::{prefix}: {slug} — delete refused, keeping its "
                "manifest row so the manifest keeps matching the bucket",
                file=sys.stderr,
            )
            undeleted.append(slug)
            continue
        swept.append(slug)

    if not dry_run and swept:
        rebuild_manifest(
            prefix, [s for s in slugs if s not in set(swept)], tmp_dir, run=run
        )

    return {
        "prefix": prefix,
        "stored": len(slugs),
        "swept": swept,
        "refused": undeleted,
        "unknown": unknown,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--prefixes",
        required=True,
        help="comma-separated S3 prefixes, e.g. conformance-dashboard,security-dashboard",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be swept and change nothing",
    )
    parser.add_argument(
        "--max-fraction",
        type=float,
        default=DEFAULT_MAX_FRACTION,
        help="refuse to sweep more than this fraction of a prefix (default 0.2)",
    )
    args = parser.parse_args(argv)

    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    if not prefixes:
        print("::error::--prefixes named no prefix", file=sys.stderr)
        return 2

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for prefix in prefixes:
            results.append(
                sweep(
                    prefix,
                    Path(tmp),
                    dry_run=args.dry_run,
                    max_fraction=args.max_fraction,
                )
            )

    for r in results:
        print(
            f"{r['prefix']}: stored={r['stored']} swept={len(r['swept'])} "
            f"refused={len(r['refused'])} unreadable={len(r['unknown'])}"
        )
    # A refusal is either safety rail firing — evidence we do not trust, or a
    # delete the bucket would not accept — so surface it as a failed run rather
    # than let a green tick read as "nothing left to clean".
    return 1 if any(r["refused"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

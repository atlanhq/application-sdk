#!/usr/bin/env python3
"""Sweep repos that no longer belong out of Kryptonite dashboard prefixes.

Every dashboard's ``repos.json`` is rebuilt by LISTING the bucket, so a repo
that should not be on a panel keeps being enumerated forever: nothing removes
its object. ``publish_fleet_dashboard.py`` now omits departed repos from the
manifest on full-fleet runs, but the per-repo publishers (security /
conformance / test-readiness, driven from each app repo's own
``update-dashboard.yml``) have no full-fleet view and cannot. This is the
deliberate, human-triggered cleanup for those — FND-960.

Two criteria, one per invocation:

**Archived** (default). Deletion is limited to repos GitHub definitively
reports as ``archived: true``:

* A repo that 404s (deleted, renamed, or invisible to this token) is REPORTED,
  never swept. The three causes need different follow-ups and a token that lost
  scope would otherwise read as "the whole fleet was deleted".
* An unreadable answer is likewise reported, never acted on.

**Not on a roster** (``--roster-file``). Deletion is limited to slugs absent
from an explicit roster of repos that belong on the panel — typically
``discover_org_consumers.py``'s output, making the sweep an exact mirror of the
publisher's own scope. GitHub is not consulted at all: the roster is the whole
question. This is what cleans up a publisher that used to write repos it should
never have written; the archived criterion cannot, since those repos are alive.

Two refusals guard it, because a roster narrower than the prefix it is applied
to names legitimate rows for deletion:

* An empty roster is REFUSED rather than read as "delete everything", for the
  same reason a 401 must not read as an empty fleet.
* Exactly ONE prefix per invocation. The dashboards do not share a publish
  scope — the gate-enforcement panel spans every repo its probe visits (~166),
  the Renovate one ~80 — so fanning one roster across prefixes would delete
  half of a healthy panel, at a fraction no cap would catch.

A delete the bucket refuses (AccessDenied, throttling) leaves the slug in
``repos.json``: the manifest tracks what is actually stored, so a row may only
disappear once its objects have.

``--max-fraction`` caps how much of a prefix one invocation may remove, because
a bad token (or a truncated roster) makes everything look sweepable and a
manifest missing most of the fleet reads as a catastrophic regression on every
panel at once. A deliberate large cleanup has to raise it explicitly, which is
the point — the operator states the expected blast radius up front.

Usage:
    sweep_dashboard_repos.py --prefixes conformance-dashboard --dry-run
    sweep_dashboard_repos.py --prefixes security-dashboard,renovate-dashboard
    sweep_dashboard_repos.py --prefixes renovate-dashboard \\
        --roster-file /tmp/fleet.json --max-fraction 0.8 --dry-run

Environment:
    GH_TOKEN  bearer token for `gh` (org read; unused in roster mode)
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


def load_roster(path: Path) -> Optional[set]:
    """Read a JSON array of ``owner/repo`` names, or None if it cannot be used.

    None (not an empty set) on every failure — missing file, malformed JSON,
    wrong shape, no entries — because a roster is the whole deletion criterion
    and an empty one would sweep the entire prefix. The caller aborts on None.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::roster {path} unreadable: {exc}", file=sys.stderr)
        return None
    if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
        print(
            f"::error::roster {path} is not a JSON array of strings",
            file=sys.stderr,
        )
        return None
    roster = {r.strip() for r in raw if r.strip()}
    if not roster:
        print(
            f"::error::roster {path} names no repo — refusing to treat an empty "
            "roster as 'sweep everything'",
            file=sys.stderr,
        )
        return None
    return roster


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


def select_archived(slugs: list, gh: GhFn) -> tuple:
    """Split *slugs* into (archived, undetermined) by asking GitHub about each."""
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
    return archived, unknown


def select_unlisted(slugs: list, roster: set) -> tuple:
    """Split *slugs* into (not on the roster, undetermined).

    No GitHub call: membership of *roster* is the entire criterion. A slug that
    does not parse as ``<owner>_<name>`` is undetermined rather than unlisted —
    it cannot be compared against a roster of ``owner/repo`` names, and an
    unexpected object in the prefix must not be deleted on the strength of a
    failed parse.
    """
    unlisted: list = []
    unknown: list = []
    for slug in slugs:
        repo = slug_to_repo(slug)
        if repo is None:
            unknown.append(slug)
        elif repo not in roster:
            unlisted.append(slug)
    return unlisted, unknown


def sweep(
    prefix: str,
    tmp_dir: Path,
    *,
    run: RunFn = _run_aws,
    gh: GhFn = _run_gh,
    dry_run: bool = False,
    max_fraction: float = DEFAULT_MAX_FRACTION,
    roster: Optional[set] = None,
) -> dict:
    """Remove departed repos' objects from *prefix* and rebuild its manifest.

    With *roster* set, "departed" means "absent from the roster"; otherwise it
    means "archived on GitHub". Everything downstream of the selection — the
    fraction cap, the delete loop, the manifest rebuild — is identical, so the
    two criteria cannot drift apart in their safety behaviour.
    """
    slugs = list_slugs(prefix, run=run)
    if roster is None:
        criterion = "archived"
        selected, unknown = select_archived(slugs, gh)
    else:
        criterion = "not on the roster"
        selected, unknown = select_unlisted(slugs, roster)

    cap = max(1, int(len(slugs) * max_fraction))
    if len(selected) > cap:
        print(
            f"::warning::{prefix}: {len(selected)} of {len(slugs)} repos read as "
            f"{criterion} (cap {cap}) — sweeping none; check the token/roster "
            "before retrying, or raise --max-fraction if this cleanup really is "
            "that large",
            file=sys.stderr,
        )
        return {
            "prefix": prefix,
            "stored": len(slugs),
            "swept": [],
            "refused": selected,
            "unknown": unknown,
        }

    for slug in unknown:
        print(
            f"::notice::{prefix}: {slug} — state unreadable, left in place",
            file=sys.stderr,
        )

    if not selected:
        print(
            f"{prefix}: nothing {criterion} among {len(slugs)} repos", file=sys.stderr
        )
        return {
            "prefix": prefix,
            "stored": len(slugs),
            "swept": [],
            "refused": [],
            "unknown": unknown,
        }

    swept: list = []
    undeleted: list = []
    for slug in selected:
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
    parser.add_argument(
        "--roster-file",
        type=Path,
        default=None,
        help="JSON array of 'owner/repo' names that BELONG on the panel (e.g. "
        "discover_org_consumers.py's output). Switches the criterion from "
        "'archived on GitHub' to 'absent from this roster', which is the only "
        "way to clean up live repos a publisher should never have written. "
        "Requires exactly one --prefixes entry, since a roster describes one "
        "publisher's scope. An empty or unreadable roster is refused, never read "
        "as 'delete all'.",
    )
    args = parser.parse_args(argv)

    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    if not prefixes:
        print("::error::--prefixes named no prefix", file=sys.stderr)
        return 2

    roster = None
    if args.roster_file is not None:
        # One prefix per roster invocation. A roster states the publish scope of
        # ONE publisher, and the dashboards do not share a scope: the
        # gate-enforcement panel covers every repo the enforcement probe visits
        # (~166), so applying the Renovate roster (~80) to it would name half of
        # a legitimate panel for deletion. That is not a cap problem — 86 of 166
        # is under any fraction an operator would raise the cap to for a genuine
        # cleanup — so the fan-out itself has to be refused.
        if len(prefixes) != 1:
            print(
                f"::error::--roster-file takes exactly one --prefixes entry, got "
                f"{len(prefixes)} ({', '.join(prefixes)}). A roster describes one "
                "publisher's scope; applying it to another dashboard would delete "
                "rows that legitimately belong there. Run one prefix at a time.",
                file=sys.stderr,
            )
            return 2
        roster = load_roster(args.roster_file)
        if roster is None:
            return 2
        print(
            f"Roster: {len(roster)} repos belong on {prefixes[0]}",
            file=sys.stderr,
        )

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for prefix in prefixes:
            results.append(
                sweep(
                    prefix,
                    Path(tmp),
                    dry_run=args.dry_run,
                    max_fraction=args.max_fraction,
                    roster=roster,
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

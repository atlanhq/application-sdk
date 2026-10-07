#!/usr/bin/env python3
"""Collect the conformance and test-readiness dashboard docs for the fleet.

The pull half of ``update-fleet-dashboards.yaml``. That scheduled job replaced
the per-repo ``update-dashboard.yml`` shim bootstrap used to install in every
connector (FND-3337). The shim fired on ``workflow_run`` completion of three
upstream workflows and called a reusable with three jobs, so every merge to
``main`` billed up to eight dashboard jobs per repo. A fleet view has no use
for per-merge freshness. One central job per interval, reading each repo's
newest artifacts, is the shape the Renovate and gate-enforcement dashboards
already use.

For every repo it reads the newest LIVE artifact on the default branch:

* the Conformance run's ``conformance-<series>-sarif`` artifacts, found and
  downloaded by ``fetch_conformance_sarif.py``, which is reused unchanged ->
  ``conformance-dashboard``
* ``test-readiness-scorecard`` (or ``-retry``) -> ``test-readiness-dashboard``

The security dashboard (Trivy scan results -> ``security-dashboard``) is no
longer collected (FND-3462). Its one known reader, connector-pulse, takes
vulnerabilities from Endor instead, so the mirror is left frozen at its last
publish.

It writes one tree per dashboard prefix in the layout
``publish_fleet_dashboard.py`` uploads::

    <out>/<prefix>/repos/<slug>.json
    <out>/<prefix>/history_<slug>.jsonl

The document shapes are the ones the reusable's inline Python produced, since
connector-pulse ingests them unchanged. The one deliberate difference is
timestamps. ``collectedAt`` and the history ``date`` now come
from the source run, not from the moment of collection. A central pull reads
the same artifact again on every tick until a newer run replaces it, so a
collection-time stamp would make a week-old scan look fresh. Stamping by
source keeps a re-read idempotent: the per-date history merge sees the same
line again.

A repo with no live artifact for a dashboard is SKIPPED for that dashboard,
not written as empty. Artifact retention is 7 days, so a repo that has not run
a workflow in that window keeps its last published row. Its timestamp shows
how old the row is, which is the honest answer for a quiet repo. Writing
zeros would read as "clean".

Per-repo failures are warned and skipped, so one unreadable repo cannot cost
the rest of the fleet its update. The run fails only when every repo failed,
because that means a token or API fault, not a fleet with nothing to report.

Extracted from inline shell per docs/standards/ci.md; unit-tested in
tests/test_fleet_dashboards_collect.py.

Environment:
    GH_TOKEN   atlan-app-fleet installation token (actions read)

Usage:
    fleet_dashboards_collect.py --repos '["atlanhq/atlan-foo-app"]' \\
        --out-dir dashboards-out
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))

import fetch_conformance_sarif as fcs  # noqa: E402
from scorecard_history_entry import history_entry as scorecard_history  # noqa: E402

# (args) -> (returncode, stdout). Same seam as fetch_conformance_sarif.run_gh,
# so one fake drives both in the tests.
GhFn = Callable[[list], tuple]

# One gh call's ceiling. The fleet is collected sequentially, so a stalled
# request with no bound would hold every later repo until the workflow's own
# timeout. Five minutes clears the largest artifact download with room.
GH_CALL_TIMEOUT_SECONDS = 300
# The exit code `timeout(1)` uses; any non-zero rc makes the caller raise.
GH_TIMEOUT_RC = 124

ARTIFACT_PAGE_SIZE = 100


def run_gh_bounded(args: list[str]) -> tuple[int, str]:
    """``fetch_conformance_sarif.run_gh`` with a per-call timeout.

    A stalled call returns a failure, so that repo's dashboard is marked
    ``error`` and the fleet scan moves on.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=GH_CALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"::warning::gh {args[0]} timed out after {GH_CALL_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return GH_TIMEOUT_RC, ""
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout


CONFORMANCE_PREFIX = "conformance-dashboard"
TEST_READINESS_PREFIX = "test-readiness-dashboard"
PREFIXES = (CONFORMANCE_PREFIX, TEST_READINESS_PREFIX)

# Plain name first, then the upload retry's name. A retried upload cannot
# reuse the first attempt's name (a failed FinalizeArtifact holds it for the
# whole run and CreateArtifact then 409s), so a run whose results landed on
# the second attempt is only reachable under the retry name.
SCORECARD_ARTIFACTS = ("test-readiness-scorecard", "test-readiness-scorecard-retry")


class CollectError(RuntimeError):
    """A read for one repo failed in a way that is not "nothing to publish"."""


def slug(repo: str) -> str:
    return repo.replace("/", "_")


def _iso_date(timestamp: str) -> str:
    """``2026-10-06T04:05:06Z`` -> ``2026-10-06``."""
    return timestamp[:10]


# ---------------------------------------------------------------------------
# GitHub reads
# ---------------------------------------------------------------------------


def default_branch(repo: str, gh: GhFn) -> str:
    rc, out = gh(["api", f"repos/{repo}", "--jq", ".default_branch"])
    branch = (out or "").strip()
    if rc != 0 or not branch:
        raise CollectError(f"could not read the default branch of {repo}")
    return branch


def latest_artifact(
    repo: str, names: tuple, branch: str, gh: GhFn
) -> Optional[dict[str, Any]]:
    """Newest non-expired artifact named one of ``names`` from a ``branch`` run.

    Queried by name rather than by listing the run history: a busy repo's PR
    runs would otherwise push the default branch's last scan out of any page
    we could afford to read. Even by name, PR-branch artifacts can fill the
    first page, so each name is paged until a page holds a match (the listing
    is newest first, so later pages are only older) or the listing runs out.
    Returns ``None`` when no live artifact exists, which is the routine case
    for a quiet repo.
    """
    found: list[dict[str, Any]] = []
    for name in names:
        page = 1
        while True:
            rc, out = gh(
                [
                    "api",
                    f"repos/{repo}/actions/artifacts?name={name}"
                    f"&per_page={ARTIFACT_PAGE_SIZE}&page={page}",
                ]
            )
            if rc != 0:
                raise CollectError(f"could not list {name} artifacts for {repo}")
            try:
                payload = json.loads(out or "{}")
            except json.JSONDecodeError as exc:
                raise CollectError(f"unparseable {name} listing for {repo}") from exc
            listed = payload.get("artifacts", []) or []
            matched = False
            for artifact in listed:
                run = artifact.get("workflow_run") or {}
                if artifact.get("expired") or run.get("head_branch") != branch:
                    continue
                if not run.get("id") or not artifact.get("created_at"):
                    continue
                found.append(artifact)
                matched = True
            if matched or len(listed) < ARTIFACT_PAGE_SIZE:
                break
            page += 1
    if not found:
        return None
    return max(found, key=lambda a: a["created_at"])


def download_artifact(repo: str, artifact: dict[str, Any], dest: Path, gh: GhFn):
    rc, _ = gh(
        [
            "run",
            "download",
            str(artifact["workflow_run"]["id"]),
            "--repo",
            repo,
            "--name",
            artifact["name"],
            "--dir",
            str(dest),
        ]
    )
    if rc != 0:
        raise CollectError(
            f"could not download {artifact['name']} from run "
            f"{artifact['workflow_run']['id']} of {repo}"
        )


def run_created_at(repo: str, run_id: int, gh: GhFn) -> tuple[str, str, str]:
    """``(head_sha, head_branch, created_at)`` of a confirmed run.

    ``fetch_conformance_sarif.head_ref`` gives the first two. The dashboard
    also needs the run's own time, so this reads all three in the same one
    call instead of making a second.
    """
    rc, out = gh(
        [
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--json",
            "headSha,headBranch,createdAt",
        ]
    )
    if rc != 0:
        raise CollectError(f"gh run view failed for run {run_id} of {repo}")
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise CollectError(f"unparseable run view for run {run_id}") from exc
    sha = payload.get("headSha") or ""
    branch = payload.get("headBranch") or ""
    created = payload.get("createdAt") or ""
    if not (sha and branch and created):
        raise CollectError(f"run {run_id} of {repo} has blank provenance")
    return sha, branch, created


# ---------------------------------------------------------------------------
# Document builders: pure, ported from update-dashboard.yaml's inline Python
# ---------------------------------------------------------------------------


def conformance_doc(
    repo: str,
    sarif_docs: list[dict[str, Any]],
    commit: str,
    branch: str,
    collected_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(repo doc, history entry)`` for the conformance dashboard."""
    tool_versions: set = set()
    profile_version = "v1"
    rule_catalog: dict[str, Any] = {}
    summary = {"failing": 0, "warning": 0, "suppressing": 0}
    by_rule: dict[str, Any] = {}
    excluded_paths: set = set()
    suppressions: list[dict[str, str]] = []

    for sarif in sarif_docs:
        for run in sarif.get("runs", []) or []:
            driver = (run.get("tool") or {}).get("driver") or {}
            if driver.get("version"):
                tool_versions.add(driver["version"])

            # Rule metadata is carried so connector-pulse never re-fetches it.
            for rule in driver.get("rules", []) or []:
                rid = rule.get("id")
                if not rid or rid in rule_catalog:
                    continue
                props = rule.get("properties") or {}
                rule_catalog[rid] = {
                    "name": rule.get("name", ""),
                    "tier": props.get("atlan/tier", ""),
                    "category": props.get("atlan/category", ""),
                    "series": props.get("atlan/series", ""),
                    "since": props.get("atlan/since", ""),
                    "helpUri": rule.get("helpUri", ""),
                    "shortDescription": (rule.get("shortDescription") or {}).get(
                        "text", ""
                    ),
                    "fullDescription": (rule.get("fullDescription") or {}).get(
                        "text", ""
                    ),
                    "rationale": props.get("atlan/rationale", ""),
                }

            run_props = run.get("properties") or {}
            run_sum = run_props.get("atlan/summary") or {}
            for key in summary:
                summary[key] += run_sum.get(key, 0)
            excluded_paths.update(run_props.get("atlan/excludedPaths", []) or [])
            if run_props.get("atlan/profileVersion"):
                profile_version = run_props["atlan/profileVersion"]

            for result in run.get("results", []) or []:
                rule_id = result.get("ruleId", "")
                if not rule_id or result.get("kind") == "pass":
                    continue
                supps = result.get("suppressions") or []
                entry = by_rule.setdefault(rule_id, {"failing": 0, "suppressing": 0})
                if supps:
                    entry["suppressing"] += 1
                    for s in supps:
                        just = (s.get("justification") or "").strip()
                        if just:
                            suppressions.append(
                                {"ruleId": rule_id, "justification": just}
                            )
                else:
                    entry["failing"] += 1

    # One toolVersion when every series agrees. Disagreement (a partial
    # publish, artifacts from two runs) is surfaced so connector-pulse can
    # flag it.
    if len(tool_versions) == 1:
        tool_version = next(iter(tool_versions))
    elif tool_versions:
        tool_version = "skew:" + ",".join(sorted(tool_versions))
    else:
        tool_version = "unknown"

    doc = {
        "repo": repo,
        "collectedAt": collected_at,
        "commit": commit,
        "branch": branch,
        "toolVersion": tool_version,
        "profileVersion": profile_version,
        "summary": summary,
        "byRule": by_rule,
        "excludedPaths": sorted(excluded_paths),
        "suppressions": suppressions,
        "ruleCatalog": rule_catalog,
    }
    history = {
        "date": _iso_date(collected_at),
        "repo": repo,
        "toolVersion": tool_version,
        "failing": summary["failing"],
        "warning": summary["warning"],
        "suppressing": summary["suppressing"],
    }
    return doc, history


# ---------------------------------------------------------------------------
# Per-repo collection
# ---------------------------------------------------------------------------


def _write(out_dir: Path, prefix: str, repo: str, doc: Any, history: dict) -> None:
    root = out_dir / prefix
    (root / "repos").mkdir(parents=True, exist_ok=True)
    (root / "repos" / f"{slug(repo)}.json").write_text(json.dumps(doc, indent=2))
    (root / f"history_{slug(repo)}.jsonl").write_text(json.dumps(history) + "\n")


def collect_conformance(
    repo: str, branch: str, out_dir: Path, work: Path, gh: GhFn
) -> bool:
    run_id, series, discovery_error = fcs.discover(
        repo, "conformance.yaml", branch, 20, gh=gh
    )
    if discovery_error:
        raise CollectError(f"conformance SARIF discovery failed for {repo}")
    if run_id is None:
        print(f"{repo}: no Conformance run with live SARIF, keeping the stored row")
        return False
    dest = work / "sarif"
    got = fcs.download(repo, run_id, series, str(dest), gh=gh)
    if not got:
        raise CollectError(f"no SARIF series retrievable from run {run_id} of {repo}")
    sha, head_branch, created = run_created_at(repo, run_id, gh)

    sarif_docs: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(dest / "*.sarif"))):
        try:
            sarif_docs.append(json.loads(Path(path).read_text()))
        except json.JSONDecodeError as exc:
            print(f"::warning::{repo}: skipping unparseable {path}: {exc}")
    # A corrupt artifact is not evidence of zero findings: publishing the
    # zero-count doc would overwrite the stored row with a false "clean".
    if not sarif_docs:
        raise CollectError(f"no parseable SARIF in run {run_id} of {repo}")

    doc, history = conformance_doc(repo, sarif_docs, sha, head_branch, created)
    _write(out_dir, CONFORMANCE_PREFIX, repo, doc, history)
    print(
        f"{repo}: conformance {len(got)}/{len(series)} series, "
        f"{doc['summary']['failing']} failing, tool {doc['toolVersion']}"
    )
    return True


def collect_test_readiness(
    repo: str, branch: str, out_dir: Path, work: Path, gh: GhFn
) -> bool:
    artifact = latest_artifact(repo, SCORECARD_ARTIFACTS, branch, gh)
    if artifact is None:
        print(f"{repo}: no live test-readiness scorecard, keeping the stored row")
        return False
    dest = work / "scorecard"
    download_artifact(repo, artifact, dest, gh)
    path = dest / "test-readiness.json"
    if not path.is_file():
        raise CollectError(f"{artifact['name']} of {repo} has no test-readiness.json")
    # Uploaded verbatim: the scorecard CLI already emits the per-repo doc.
    scorecard = json.loads(path.read_text())
    history = scorecard_history(scorecard, _iso_date(artifact["created_at"]))
    _write(out_dir, TEST_READINESS_PREFIX, repo, scorecard, history)
    print(f"{repo}: test-readiness score {history['score']} grade {history['grade']}")
    return True


def collect_repo(repo: str, out_dir: Path, gh: GhFn) -> dict[str, str]:
    """Collect every dashboard for ``repo``.

    Returns ``{prefix: "published" | "skipped" | "error"}``. Each dashboard is
    collected independently, so a broken scorecard does not cost the repo its
    conformance row.
    """
    branch = default_branch(repo, gh)
    outcome: dict[str, str] = {}
    collectors = {
        CONFORMANCE_PREFIX: lambda w: collect_conformance(repo, branch, out_dir, w, gh),
        TEST_READINESS_PREFIX: lambda w: collect_test_readiness(
            repo, branch, out_dir, w, gh
        ),
    }
    for prefix, collect in collectors.items():
        with tempfile.TemporaryDirectory() as work:
            try:
                outcome[prefix] = "published" if collect(Path(work)) else "skipped"
            except (CollectError, OSError, json.JSONDecodeError) as exc:
                print(f"::warning::{repo}: {prefix} not collected: {exc}")
                outcome[prefix] = "error"
    return outcome


def collect_fleet(
    repos: list[str], out_dir: Path, gh: GhFn = run_gh_bounded
) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for repo in repos:
        try:
            results[repo] = collect_repo(repo, out_dir, gh)
        except CollectError as exc:
            print(f"::warning::{repo}: skipped entirely: {exc}")
            results[repo] = {prefix: "error" for prefix in PREFIXES}
    return results


def all_failed(results: dict[str, dict[str, str]]) -> bool:
    """True when nothing was read at all, which is a token or API fault."""
    return bool(results) and all(
        status == "error" for outcome in results.values() for status in outcome.values()
    )


def main(argv: Optional[list] = None, gh: GhFn = run_gh_bounded) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repos", required=True, help='JSON list of "owner/name"')
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    repos = json.loads(args.repos)
    if not isinstance(repos, list) or not repos:
        print("::error::--repos must be a non-empty JSON list", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    results = collect_fleet(repos, args.out_dir, gh=gh)

    for prefix in PREFIXES:
        counts: dict[str, int] = {}
        for outcome in results.values():
            counts[outcome[prefix]] = counts.get(outcome[prefix], 0) + 1
        print(f"{prefix}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write("| Repo | Conformance | Test readiness |\n")
            fh.write("| --- | --- | --- |\n")
            for repo, outcome in sorted(results.items()):
                cells = " | ".join(outcome[p] for p in PREFIXES)
                fh.write(f"| {repo} | {cells} |\n")

    if all_failed(results):
        print(
            "::error::every repo failed to collect — token scope or API fault, "
            "not an empty fleet",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

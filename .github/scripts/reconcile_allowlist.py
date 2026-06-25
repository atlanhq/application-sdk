#!/usr/bin/env python3
"""Reconcile the allowlist + tickets against what the scanner still sees.

Closing the loop on the zero-human-touch CVE flow: once a CVE stops showing up
in the scan (its bump merged & shipped, or the base image was rebuilt), its
allowlist entry and Linear ticket should be retired automatically.

**3-consecutive-clean-scan debounce.** A CVE is treated as *resolved* only if it
is absent from the current scan AND the previous two hourly scans (three runs in
agreement). CVEs vanish transiently — Trivy DB reclassification, a partial scan,
or the `uv sync` native-lockfile step not materializing a wheel — so a single
clean scan is not enough to yank a valid suppression.

For each resolved CVE:
  * if it has an open allowlist entry → open an **allowlist removal PR** (touches
    only `.security/base-allowlist.json`, label `vuln-auto-merge` → auto-merges
    via vuln-auto-merge.yml as `atlan-ci`).
  * a Linear ticket is closed only when ALL the CVEs it tracks are resolved;
    otherwise its marker is rewritten to drop just the resolved CVE.

Pure decision logic (the debounce + ticket plan) lives in tested functions;
GitHub/Linear I/O is wired in main(), per docs/standards/ci.md.

Environment:
    REPO                 owner/name (default atlanhq/application-sdk)
    GH_TOKEN             atlan-ci PAT (push branch + open the removal PR)
    LINEAR_API_KEY       to close / update tickets (optional; skipped if unset)
    SCAN_WORKFLOW        default 'daily-security-scan.yml'
    RUN_DATE             ISO date, for branch/commit naming

Optional:
    LINEAR_VULN_LABEL_NAME  default 'vulnerabilities'
    DEBOUNCE_SCANS          default 3 (this scan + N-1 prior)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import security_scan_create_linear as ssl  # reuse Linear helpers

Runner = Callable[..., subprocess.CompletedProcess]

ALLOWLIST_PATH = ".security/base-allowlist.json"
TRIVY_FILES = ["trivy-image-results.json", "trivy-fs-results.json"]
DEFAULT_WORKFLOW = "daily-security-scan.yml"


# ---------------------------------------------------------------------------
# Pure logic (tested)
# ---------------------------------------------------------------------------


def cve_ids_from_trivy(data: dict) -> set[str]:
    """Every VulnerabilityID across a parsed Trivy JSON document."""
    ids: set[str] = set()
    for result in data.get("Results", []) or []:
        for vuln in result.get("Vulnerabilities", []) or []:
            vid = vuln.get("VulnerabilityID")
            if vid:
                ids.add(vid)
    return ids


def resolved_cves(
    allowlisted: set[str],
    scan_sets: list[set[str]],
    debounce: int,
) -> set[str]:
    """Allowlisted CVEs absent from EVERY one of the last ``debounce`` scans.

    ``scan_sets`` is the CVE set per recent scan (current + priors). If we have
    fewer than ``debounce`` scans we cannot confirm the debounce, so nothing is
    resolved (fail-safe — never yank a suppression on thin evidence).
    """
    if len([s for s in scan_sets if s is not None]) < debounce:
        return set()
    seen_in_any = set().union(*scan_sets) if scan_sets else set()
    return {cve for cve in allowlisted if cve not in seen_in_any}


def plan_ticket_updates(
    tickets: list[dict], resolved: set[str]
) -> tuple[list[dict], list[dict]]:
    """Split open tickets into (to_close, to_update).

    A ticket's tracked CVEs come from its ``<!-- vuln-ids: ... -->`` marker.
    Close it when every tracked CVE is resolved; otherwise rewrite the marker to
    drop the resolved ones. Tickets with no resolved CVEs are left untouched.
    """
    to_close: list[dict] = []
    to_update: list[dict] = []
    for t in tickets:
        tracked = ssl.extract_vuln_ids_from_description(t.get("description"))
        if not tracked:
            continue
        resolved_here = tracked & resolved
        if not resolved_here:
            continue
        remaining = tracked - resolved
        if not remaining:
            to_close.append(t)
        else:
            to_update.append({**t, "remaining_ids": sorted(remaining)})
    return to_close, to_update


# ---------------------------------------------------------------------------
# GitHub I/O
# ---------------------------------------------------------------------------


def load_current_cves() -> set[str]:
    ids: set[str] = set()
    for fname in TRIVY_FILES:
        p = Path(fname)
        if p.exists():
            try:
                ids |= cve_ids_from_trivy(json.loads(p.read_text()))
            except json.JSONDecodeError:
                print(f"warning: {fname} invalid JSON; skipping")
    return ids


def load_prior_scan_cves(
    repo: str, workflow: str, count: int, runner: Runner
) -> list[set[str]]:
    """Download the ``count`` most recent prior successful scan artifacts and
    return their CVE sets. Best-effort: runs without the artifact are skipped."""
    listing = runner(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--workflow",
            workflow,
            "--status",
            "success",
            "--limit",
            str(count + 2),
            "--json",
            "databaseId",
            "--jq",
            ".[].databaseId",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0 or not (listing.stdout or "").strip():
        return []
    run_ids = listing.stdout.split()
    sets: list[set[str]] = []
    for rid in run_ids:
        if len(sets) >= count:
            break
        with tempfile.TemporaryDirectory() as td:
            dl = runner(
                [
                    "gh",
                    "run",
                    "download",
                    rid,
                    "-R",
                    repo,
                    "-n",
                    "security-scan-raw-results",
                    "-D",
                    td,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if dl.returncode != 0:
                continue
            ids: set[str] = set()
            for fname in TRIVY_FILES:
                p = Path(td) / fname
                if p.exists():
                    try:
                        ids |= cve_ids_from_trivy(json.loads(p.read_text()))
                    except json.JSONDecodeError:
                        pass
            sets.append(ids)
    return sets


def open_removal_pr(
    repo: str, removed: list[str], run_date: str, runner: Runner
) -> None:
    """Drop resolved entries from the allowlist and open an auto-merge PR."""
    data = json.loads(Path(ALLOWLIST_PATH).read_text())
    for cve in removed:
        data.pop(cve, None)
    Path(ALLOWLIST_PATH).write_text(json.dumps(data, indent=2) + "\n")

    # GITHUB_RUN_ID keeps the branch unique across same-day reconcile runs.
    suffix = os.environ.get("GITHUB_RUN_ID", run_date) or run_date
    branch = f"chore/allowlist-remove-{suffix}"
    joined = ", ".join(removed)
    runner(["git", "checkout", "-b", branch], check=True)
    runner(["git", "add", ALLOWLIST_PATH], check=True)
    runner(
        [
            "git",
            "commit",
            "-m",
            f"chore(security): remove resolved allowlist entries ({joined})",
        ],
        check=True,
    )
    runner(["git", "push", "origin", branch], check=True)
    runner(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            "main",
            "--title",
            f"chore(security): remove {len(removed)} resolved allowlist entr"
            + ("y" if len(removed) == 1 else "ies"),
            "--label",
            "vuln-auto-merge",
            "--body",
            "Resolved by 3 consecutive clean scans (scanner no longer reports "
            "these CVEs):\n\n" + "\n".join(f"- `{c}`" for c in removed),
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Linear I/O
# ---------------------------------------------------------------------------


def resolve_done_state_id(team_id: str) -> str | None:
    data = ssl.gql(
        """
        query TeamStates($teamId: String!) {
          team(id: $teamId) { states { nodes { id name type } } }
        }
        """,
        {"teamId": team_id},
    )
    states = ((data.get("team") or {}).get("states") or {}).get("nodes") or []
    for s in states:
        if s.get("type") == "completed":
            return s["id"]
    return None


def close_ticket(issue_id: str, state_id: str) -> None:
    ssl.gql(
        """
        mutation Close($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) { success }
        }
        """,
        {"id": issue_id, "stateId": state_id},
    )


def rewrite_marker(issue_id: str, description: str, remaining_ids: list[str]) -> None:
    marker = (
        f"{ssl.VULN_MARKER_PREFIX}{','.join(remaining_ids)}{ssl.VULN_MARKER_SUFFIX}"
    )
    new_desc = []
    replaced = False
    for line in (description or "").splitlines():
        s = line.strip()
        if s.startswith(ssl.VULN_MARKER_PREFIX) and s.endswith(ssl.VULN_MARKER_SUFFIX):
            new_desc.append(marker)
            replaced = True
        else:
            new_desc.append(line)
    if not replaced:
        new_desc.append(marker)
    ssl.gql(
        """
        mutation Update($id: String!, $desc: String!) {
          issueUpdate(id: $id, input: { description: $desc }) { success }
        }
        """,
        {"id": issue_id, "desc": "\n".join(new_desc)},
    )


def reconcile_tickets(resolved: set[str]) -> None:
    if not os.environ.get("LINEAR_API_KEY") or not os.environ.get("LINEAR_TEAM_ID"):
        print("LINEAR_API_KEY/TEAM_ID not set — skipping ticket reconciliation.")
        return
    team_id = os.environ["LINEAR_TEAM_ID"]
    label_name = os.environ.get("LINEAR_VULN_LABEL_NAME", "vulnerabilities")
    label_id = ssl.resolve_label_id(label_name, team_id)
    if not label_id:
        print(f"label {label_name!r} not found — skipping ticket reconciliation.")
        return
    tickets = ssl.fetch_open_labelled_issues(label_id)
    to_close, to_update = plan_ticket_updates(tickets, resolved)
    done_state = resolve_done_state_id(team_id) if to_close else None
    for t in to_close:
        if done_state:
            close_ticket(t["id"], done_state)
            print(f"Closed {t['identifier']} (all tracked CVEs resolved).")
    for t in to_update:
        rewrite_marker(t["id"], t.get("description", ""), t["remaining_ids"])
        print(f"Updated {t['identifier']} marker (dropped resolved CVEs).")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def load_allowlisted_cves() -> set[str]:
    try:
        data = json.loads(Path(ALLOWLIST_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return {k for k in data if not k.startswith("_")}


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ.get("REPO", "atlanhq/application-sdk")
    workflow = os.environ.get("SCAN_WORKFLOW", DEFAULT_WORKFLOW)
    run_date = os.environ.get("RUN_DATE", "")
    debounce = int(os.environ.get("DEBOUNCE_SCANS", "3"))

    allowlisted = load_allowlisted_cves()
    if not allowlisted:
        print("Allowlist has no entries — nothing to reconcile.")
        return 0

    current = load_current_cves()
    priors = load_prior_scan_cves(repo, workflow, debounce - 1, runner)
    scan_sets = [current, *priors]

    resolved = resolved_cves(allowlisted, scan_sets, debounce)
    if not resolved:
        print(
            f"No allowlisted CVE absent across {debounce} consecutive scans "
            f"(have {len(scan_sets)} scan set(s)). Nothing to remove."
        )
        return 0

    removed = sorted(resolved)
    print(f"Resolved (gone for {debounce} scans): {', '.join(removed)}")
    open_removal_pr(repo, removed, run_date, runner)
    reconcile_tickets(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Reconcile the allowlist + tickets against what the scanner still sees.

Closing the loop on the zero-human-touch CVE flow: once a CVE stops showing up
in the scan (its bump merged & shipped, or the base image was rebuilt), its
allowlist entry and Linear ticket should be retired automatically.

Invoked on a stable SDK *release* (`release: released` —
vuln-reconcile-on-release.yml), NOT on the hourly scan. The allowlist is shared
across connector repos whose build gates read it from SDK `main` while their
images still carry the CVE from the last released SDK — so an entry must persist
until the fix is released, not merely merged. The hourly scan still
detects/tickets/allowlists; only this retirement step is release-gated.

The *current* scan is produced fresh by the release workflow (the shared Trivy
composite, run against `main`) and read from the CWD. The debounce *window*
(prior scans, only consulted when ``DEBOUNCE_SCANS`` > 1) is pinned to ``main``
via ``SCAN_BRANCH`` so an hourly-scan ``workflow_dispatch`` from a feature branch
can never feed reconcile a view of non-released code (and wrongly retire entries
from the shared allowlist).

**Resolution & debounce.** By default a CVE is treated as *resolved* as soon as
it is absent from the latest scan (`DEBOUNCE_SCANS` = 1). Raise `DEBOUNCE_SCANS`
to require it to be absent from that many consecutive successful scans before
acting — a debounce against transient drops (Trivy DB reclassification, a partial
scan, or the `uv sync` native-lockfile step not materializing a wheel).

Two INDEPENDENT actions run off the same scan history (don't couple them):
  * **Allowlist removal PR** — for *allowlisted* CVEs gone for `debounce` scans:
    open a removal PR (touches only `.security/base-allowlist.json`, label
    `vuln-auto-merge` → auto-merges via vuln-auto-merge.yml as `atlan-ci`).
  * **Ticket reconciliation** — for any open `vulnerabilities` ticket whose
    tracked CVEs are gone for `debounce` scans, close it (or trim its marker if
    only some are gone). This is keyed on the SCAN, NOT the allowlist — so
    Medium/Low tickets (which are never allowlisted) and any CVE fixed before it
    was ever allowlisted still get closed. Reconciliation runs even when the
    allowlist is empty.

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
    DEBOUNCE_SCANS          default 1 — act on the first clean scan (this scan +
                            N-1 prior successful scans); raise to debounce churn
    SCAN_BRANCH             default 'main' — branch whose scan runs feed the
                            debounce window (excludes feature-branch dispatches)
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
# Act on the first clean scan by default; both the allowlist removal PR and the
# ticket close are gated on this same window. Raise via DEBOUNCE_SCANS to require
# N consecutive clean scans (debounce against transient scanner drops).
DEFAULT_DEBOUNCE = 1


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
    candidates: set[str],
    scan_sets: list[set[str]],
    debounce: int,
) -> set[str]:
    """CVEs from ``candidates`` absent from EVERY one of the last ``debounce``
    scans. Used for both the allowlist (candidates = allowlisted CVEs) and ticket
    closing (candidates = CVEs tracked by open tickets).

    ``scan_sets`` is the CVE set per recent scan (current + priors). If we have
    fewer than ``debounce`` scans we cannot confirm the debounce, so nothing is
    resolved (fail-safe — never act on thin evidence).
    """
    if len(scan_sets) < debounce:
        return set()
    seen_in_any = set().union(*scan_sets) if scan_sets else set()
    return {cve for cve in candidates if cve not in seen_in_any}


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


def resolve_tickets(
    tickets: list[dict], scan_sets: list[set[str]], debounce: int
) -> tuple[list[dict], list[dict]]:
    """Decide which open tickets to close/update based purely on whether their
    tracked CVEs are gone for ``debounce`` scans — INDEPENDENT of the allowlist,
    so Medium/Low tickets (never allowlisted) and CVEs fixed before they were
    ever allowlisted still get closed."""
    all_tracked: set[str] = set()
    for t in tickets:
        all_tracked |= ssl.extract_vuln_ids_from_description(t.get("description"))
    resolved = resolved_cves(all_tracked, scan_sets, debounce)
    return plan_ticket_updates(tickets, resolved)


# ---------------------------------------------------------------------------
# GitHub I/O
# ---------------------------------------------------------------------------


def scan_files_present() -> bool:
    """True if at least one Trivy result file is staged in the CWD.

    Reconcile MUST NOT run against a phantom "empty scan": an absent artifact
    (e.g. a failed download in the release workflow) would otherwise look like a
    scan that found zero CVEs and resolve — i.e. delete — every allowlist entry.
    main() bails when this is False.
    """
    return any(Path(f).exists() for f in TRIVY_FILES)


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
    repo: str, workflow: str, count: int, runner: Runner, branch: str = "main"
) -> list[set[str]]:
    """Download the ``count`` most recent prior successful scan artifacts and
    return their CVE sets. Best-effort: runs without the artifact are skipped.

    Pinned to ``branch`` (default ``main``): a scan ``workflow_dispatch`` from a
    feature branch must never feed the debounce window — only runs of ``main``
    reflect what is actually shipped. (Not ``--event schedule``, so a manual
    dispatch on ``main`` for backfill still counts.)"""
    if count <= 0:
        # debounce=1 (the default): the fresh current scan is the only evidence;
        # no prior window needed, so skip the API call (and the actions: read it
        # would require).
        return []
    listing = runner(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--workflow",
            workflow,
            "--branch",
            branch,
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


def _resolution_phrase(debounce: int) -> str:
    """Honest rationale string for any DEBOUNCE_SCANS value."""
    if debounce <= 1:
        return "the latest clean scan"
    return f"{debounce} consecutive clean scans"


def open_removal_pr(
    repo: str, removed: list[str], run_date: str, debounce: int, runner: Runner
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
            "The latest SDK release no longer ships these CVEs (confirmed by "
            f"{_resolution_phrase(debounce)}):\n\n"
            + "\n".join(f"- `{c}`" for c in removed),
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


def close_ticket(issue_id: str, state_id: str) -> bool:
    data = ssl.gql(
        """
        mutation Close($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) { success }
        }
        """,
        {"id": issue_id, "stateId": state_id},
    )
    return bool((data.get("issueUpdate") or {}).get("success"))


def comment_on_ticket(issue_id: str, body: str) -> None:
    ssl.gql(
        """
        mutation Comment($input: CommentCreateInput!) {
          commentCreate(input: $input) { success }
        }
        """,
        {"input": {"issueId": issue_id, "body": body}},
    )


def _run_url() -> str:
    """The reconcile workflow run URL, from GitHub-provided env (empty locally)."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def close_comment_body(cves: list[str], run_url: str) -> str:
    """The explanatory comment posted on a ticket as it is auto-closed."""
    plural = "y" if len(cves) == 1 else "ies"
    cve_list = ", ".join(f"`{c}`" for c in sorted(cves))
    lines = [
        "**Auto-resolved on SDK release.**",
        "",
        f"A new SDK release no longer ships the tracked vulnerabilit{plural} "
        f"({cve_list}), so this ticket is being closed automatically. If the "
        "finding resurfaces, the next scan files a fresh ticket.",
    ]
    if run_url:
        lines += ["", f"Reconcile run: {run_url}"]
    return "\n".join(lines)


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


def reconcile_tickets(scan_sets: list[set[str]], debounce: int) -> None:
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
    to_close, to_update = resolve_tickets(tickets, scan_sets, debounce)
    if not to_close and not to_update:
        print("No open ticket has its tracked CVE(s) resolved — nothing to close.")
        return
    done_state = resolve_done_state_id(team_id) if to_close else None
    run_url = _run_url()
    for t in to_close:
        if not done_state:
            print(
                f"Could not resolve a completed state — leaving {t['identifier']} open."
            )
            continue
        # Close FIRST; only comment once the close succeeds. If the close fails we
        # post nothing (no self-contradicting "closing" note), and if the comment
        # later fails the ticket is already closed so the next run won't re-close
        # or duplicate it.
        if not close_ticket(t["id"], done_state):
            print(f"Failed to close {t['identifier']} — will retry next run.")
            continue
        cves = sorted(ssl.extract_vuln_ids_from_description(t.get("description")))
        comment_on_ticket(t["id"], close_comment_body(cves, run_url))
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
    branch = os.environ.get("SCAN_BRANCH", "main")
    debounce = int(os.environ.get("DEBOUNCE_SCANS", str(DEFAULT_DEBOUNCE)))

    if not scan_files_present():
        print(
            "No Trivy scan results staged in the working dir "
            f"({', '.join(TRIVY_FILES)}) — refusing to reconcile against an empty "
            "scan (fail-safe; would otherwise resolve every allowlist entry). "
            "Nothing changed."
        )
        return 0

    current = load_current_cves()
    priors = load_prior_scan_cves(repo, workflow, debounce - 1, runner, branch)
    scan_sets = [current, *priors]
    if len(scan_sets) < debounce:
        print(
            f"Only {len(scan_sets)} scan(s) of history (<{debounce}) — "
            "skipping reconciliation (fail-safe)."
        )
        return 0

    # 1) Allowlist removal PR — scoped to *allowlisted* CVEs gone for `debounce`
    #    scans. (No early return on an empty allowlist — tickets still reconcile.)
    allowlisted = load_allowlisted_cves()
    resolved_allowlisted = resolved_cves(allowlisted, scan_sets, debounce)
    if resolved_allowlisted:
        removed = sorted(resolved_allowlisted)
        print(
            f"Allowlist entries resolved (gone for {debounce} scans): {', '.join(removed)}"
        )
        open_removal_pr(repo, removed, run_date, debounce, runner)
    else:
        print(
            f"No allowlisted CVE absent across {debounce} scans "
            f"({len(allowlisted)} allowlist entr{'y' if len(allowlisted) == 1 else 'ies'}) "
            "— no removal PR."
        )

    # 2) Ticket reconciliation — keyed on the SCAN, independent of the allowlist,
    #    so Medium/Low and pre-allowlist tickets are closed too.
    reconcile_tickets(scan_sets, debounce)
    return 0


if __name__ == "__main__":
    sys.exit(main())

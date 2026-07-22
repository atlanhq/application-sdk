#!/usr/bin/env python3
"""Create Linear tickets for daily security scan findings, with dedup.

One ticket is filed **per severity level** (Critical / High / Medium / Low)
so each carries its own remediation SLA and priority and is triaged
independently by the vuln-triage rover.

Compared to a naive "create one ticket per run":
- Every finding (all severities) is identified by a stable ID (Trivy CVE).
- All previously-created issues are tagged with the ``vulnerabilities``
  label. We fetch every open issue carrying that label and look for a
  hidden marker line in each description:
      <!-- vuln-ids: id1,id2,id3 -->
  IDs already covered by an open labelled issue are removed from
  today's set (dedup is global — a CVE tracked by any open ticket of any
  severity is not refiled).
- If every finding is already covered, no new issue is created — we
  just record that fact in the workflow summary.
- Otherwise the remaining *new* findings are partitioned by severity and
  one ticket per non-empty severity is created, tagged with the same label
  so the next run dedups against it. Each ticket's marker lists only the
  CVE IDs of its own severity (the ones it tracks).

Each new issue is created in the team's "Backlog" state (not Triage).
The ``Backlog`` state is resolved at runtime from the team's workflow.

The label name defaults to ``vulnerabilities`` and must already exist
in Linear (team-scoped or workspace-level). The script fails loud if
the label is missing rather than silently skipping dedup.

Required env vars:
    LINEAR_API_KEY        Personal API key
    LINEAR_TEAM_ID        Team UUID
    LINEAR_PROJECT_ID     Project UUID
    SCAN_DATE             ISO date
    RUN_URL               GitHub Actions run URL
    TARGET_IMAGE          Image scanned

Optional:
    LINEAR_MILESTONE_ID
    LINEAR_ASSIGNEE_ID
    LINEAR_VULN_LABEL_NAME  default: "vulnerabilities"
    GITHUB_STEP_SUMMARY     Markdown is appended for visibility
    GITHUB_OUTPUT           ``new_issues`` (JSON array of {identifier, url,
                            severity}) is emitted here for the dispatch step
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LINEAR_URL = "https://api.linear.app/graphql"

TRIVY_FILES = ["trivy-image-results.json", "trivy-fs-results.json"]

# All severities are ticketed. The hourly scan no longer runs with
# --ignore-unfixed, so findings that reach here may OR may not have an upstream
# fix available. A fix, when one exists, is delivered either as a dependency
# bump (Case 1, our dep) OR a base-image rebuild (Case 4, a package baked into
# app-runtime-base, e.g. Dapr). No-fix CVEs (our-dep-no-fix and disputed/
# won't-fix) now reach the rover too, so its allowlist/alternative branches
# (Cases 2/3) are fully reachable — those are exactly the findings that need an
# operational mitigation or an explicit allowlist decision rather than a bump.
# The rover classifies each finding from the ticket — see
# .mothership/vuln-triage/ORCHESTRATION.md.
ACTIONABLE_SEVERITIES_TRIVY = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}

# Lower rank sorts first.
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# One ticket is filed per severity level so each carries the right SLA and
# priority and is triaged independently. Map each severity to a Linear
# priority (1=Urgent … 4=Low) and a human label for the title.
SEVERITY_PRIORITY = {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 4}
SEVERITY_LABEL = {
    "CRITICAL": "Critical",
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
}


def gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ["LINEAR_API_KEY"]
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"Linear API HTTP {e.code}: {e.read().decode()}\n")
        raise
    if "errors" in payload:
        raise RuntimeError(f"Linear API errors: {json.dumps(payload['errors'])}")
    return payload["data"]


def collect_findings() -> list[dict]:
    """Build the list of findings (all severities) across Trivy scanners.

    Each finding is a dict with id, severity, package, version, source.
    """
    findings: dict[str, dict] = {}

    for fname in TRIVY_FILES:
        path = Path(fname)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"warning: {fname} is not valid JSON; skipping")
            continue
        for result in data.get("Results", []) or []:
            for vuln in result.get("Vulnerabilities", []) or []:
                if vuln.get("Severity", "").upper() not in ACTIONABLE_SEVERITIES_TRIVY:
                    continue
                vid = vuln.get("VulnerabilityID")
                if not vid or vid in findings:
                    continue
                findings[vid] = {
                    "id": vid,
                    "severity": vuln.get("Severity", "").upper(),
                    "package": vuln.get("PkgName", ""),
                    "version": vuln.get("InstalledVersion", ""),
                    "fixed": vuln.get("FixedVersion") or "N/A",
                    "title": vuln.get("Title", ""),
                    "source": "trivy",
                    "scanner_target": fname,
                }

    return sorted(
        findings.values(),
        key=lambda v: (SEVERITY_RANK.get(v["severity"], 99), v["id"]),
    )


VULN_MARKER_PREFIX = "<!-- vuln-ids: "
VULN_MARKER_SUFFIX = " -->"


def extract_vuln_ids_from_description(description: str | None) -> set[str]:
    if not description:
        return set()
    for line in description.splitlines():
        s = line.strip()
        if s.startswith(VULN_MARKER_PREFIX) and s.endswith(VULN_MARKER_SUFFIX):
            inner = s[len(VULN_MARKER_PREFIX) : -len(VULN_MARKER_SUFFIX)]
            return {part.strip() for part in inner.split(",") if part.strip()}
    return set()


def resolve_label_id(label_name: str, team_id: str) -> str | None:
    """Find an IssueLabel by name. Prefer one scoped to ``team_id``; fall
    back to a workspace-level label with the same name; else None.
    """
    data = gql(
        """
        query FindLabel($name: String!) {
          issueLabels(filter: { name: { eq: $name } }) {
            nodes { id name team { id } }
          }
        }
        """,
        {"name": label_name},
    )
    nodes = ((data.get("issueLabels") or {}).get("nodes")) or []
    if not nodes:
        return None
    for n in nodes:
        if (n.get("team") or {}).get("id") == team_id:
            return n["id"]
    for n in nodes:
        if not n.get("team"):
            return n["id"]
    return nodes[0]["id"]


def fetch_open_labelled_issues(label_id: str) -> list[dict]:
    """Pull open (non-completed, non-canceled) issues carrying the given
    label. Paginates if there are more than 100 matches. State filtering
    is done client-side.
    """
    issues: list[dict] = []
    after: str | None = None
    closed_types = {"completed", "canceled"}
    while True:
        data = gql(
            """
            query OpenLabelled($labelId: ID!, $after: String) {
              issues(
                first: 100,
                after: $after,
                filter: { labels: { some: { id: { eq: $labelId } } } }
              ) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  identifier
                  url
                  title
                  description
                  state { type }
                }
              }
            }
            """,
            {"labelId": label_id, "after": after},
        )
        page = data.get("issues") or {}
        for node in page.get("nodes") or []:
            if (node.get("state") or {}).get("type") in closed_types:
                continue
            issues.append(node)
        if not page.get("pageInfo", {}).get("hasNextPage"):
            break
        after = page["pageInfo"]["endCursor"]
    return issues


def resolve_backlog_state_id(team_id: str) -> str | None:
    """Find the team's "Backlog" workflow state, falling back to the
    first state of type ``backlog``.
    """
    data = gql(
        """
        query TeamStates($teamId: String!) {
          team(id: $teamId) {
            states { nodes { id name type } }
          }
        }
        """,
        {"teamId": team_id},
    )
    states = ((data.get("team") or {}).get("states") or {}).get("nodes") or []
    for s in states:
        if (s.get("name") or "").strip().lower() == "backlog":
            return s["id"]
    for s in states:
        if s.get("type") == "backlog":
            return s["id"]
    return None


def render_table(findings: list[dict]) -> str:
    if not findings:
        return ""
    lines = [
        "| Severity | Scanner | Package | Version | Fixed In | ID |",
        "|----------|---------|---------|---------|----------|----|",
    ]
    sev_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}
    for f in findings:
        emoji = sev_emoji.get(f["severity"], "")
        lines.append(
            "| {emoji} {sev} | {src} | `{pkg}` | `{ver}` | {fix} | `{vid}` |".format(
                emoji=emoji,
                sev=f["severity"],
                src=f["source"],
                pkg=f["package"] or "?",
                ver=f["version"] or "?",
                fix=f["fixed"],
                vid=f["id"],
            )
        )
    return "\n".join(lines)


def build_description(
    new_findings: list[dict],
    severity: str,
    target_image: str,
    scan_date: str,
    run_url: str,
) -> str:
    """Render a per-severity ticket body. The marker lists only this
    severity's CVE IDs (the ones this ticket tracks)."""
    new_ids = [f["id"] for f in new_findings]
    visible_ids = "\n".join(f"- `{vid}`" for vid in new_ids)
    table = render_table(new_findings)
    marker = f"{VULN_MARKER_PREFIX}{','.join(new_ids)}{VULN_MARKER_SUFFIX}"
    plural = "y" if len(new_findings) == 1 else "ies"
    sev_label = SEVERITY_LABEL.get(severity, severity.title())
    return f"""## Daily Security Scan — New {sev_label} Findings

**Image:** `{target_image}`
**Python deps:** `pyproject.toml` / `uv.lock` (application-sdk)
**Scan date:** {scan_date}
**Workflow run:** [View logs]({run_url})

This issue tracks **{len(new_findings)} new {sev_label}** vulnerabilit{plural} \
not already covered by an open Linear issue in this project.

---

### New Vulnerability IDs

{visible_ids}

### Details

{table}

---

### Tracked IDs (machine-readable)

The next daily scan will skip ticket creation if every finding it sees \
is already listed (across this issue and any other open issue). To stop \
deduping a finding, close or cancel its issue.

{marker}

---

*Automatically created by the daily security scan workflow.*
"""


def write_summary(line: str) -> None:
    print(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(line + "\n")


def emit_output(name: str, value: str) -> None:
    """Write a step output to ``$GITHUB_OUTPUT`` so later jobs can consume it."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a") as f:
            f.write(f"{name}={value}\n")


def create_issue(
    severity: str,
    bucket: list[dict],
    *,
    team_id: str,
    project_id: str,
    label_id: str,
    backlog_state_id: str | None,
    target_image: str,
    scan_date: str,
    run_url: str,
) -> dict | None:
    """Create one per-severity Linear issue. Returns {identifier, url,
    severity} on success, else None."""
    sev_label = SEVERITY_LABEL.get(severity, severity.title())
    description = build_description(bucket, severity, target_image, scan_date, run_url)
    title = (
        f"[Security][{sev_label}] {len(bucket)} new "
        f"vulnerabilit{'y' if len(bucket) == 1 else 'ies'} ({scan_date})"
    )

    issue_input: dict[str, Any] = {
        "title": title,
        "description": description,
        "teamId": team_id,
        "projectId": project_id,
        "priority": SEVERITY_PRIORITY.get(severity, 2),
        "labelIds": [label_id],
    }
    for env_key, payload_key in [
        ("LINEAR_MILESTONE_ID", "projectMilestoneId"),
        ("LINEAR_ASSIGNEE_ID", "assigneeId"),
    ]:
        val = os.environ.get(env_key)
        if val:
            issue_input[payload_key] = val
    if backlog_state_id:
        issue_input["stateId"] = backlog_state_id

    data = gql(
        """
        mutation CreateIssue($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { id identifier url }
          }
        }
        """,
        {"input": issue_input},
    )
    result = data.get("issueCreate") or {}
    if not result.get("success"):
        sys.stderr.write(
            f"Linear issueCreate returned success=false for {severity}: "
            f"{json.dumps(data)}\n"
        )
        return None

    issue = result.get("issue") or {}
    write_summary(
        f"### Linear Issue ({sev_label}): "
        f"[{issue.get('identifier')}]({issue.get('url')})"
    )
    return {
        "identifier": issue.get("identifier") or "",
        "url": issue.get("url") or "",
        "severity": severity,
    }


def main() -> int:
    findings = collect_findings()
    if not findings:
        write_summary("> ✅ No findings — no Linear issue created.")
        return 0

    today_ids = [f["id"] for f in findings]
    print(f"Today's findings (all severities): {len(today_ids)}")
    for vid in today_ids:
        print(f"  - {vid}")

    team_id = os.environ["LINEAR_TEAM_ID"]
    project_id = os.environ["LINEAR_PROJECT_ID"]
    label_name = os.environ.get("LINEAR_VULN_LABEL_NAME", "vulnerabilities")

    label_id = resolve_label_id(label_name, team_id)
    if not label_id:
        sys.stderr.write(
            f"::error::Linear label {label_name!r} not found. Create it in the "
            f"Linear team workspace before this workflow runs again.\n"
        )
        return 1
    print(f"Using label {label_name!r} (id={label_id}) for dedup + new issue.")

    open_issues = fetch_open_labelled_issues(label_id)
    print(f"Open issues with label {label_name!r}: {len(open_issues)}")

    covered: dict[str, dict] = {}  # vuln_id -> issue node
    for issue in open_issues:
        for vid in extract_vuln_ids_from_description(issue.get("description")):
            covered.setdefault(vid, issue)

    new_findings = [f for f in findings if f["id"] not in covered]
    already_tracked = [f for f in findings if f["id"] in covered]

    if already_tracked:
        write_summary(
            f"> ℹ️ {len(already_tracked)} finding(s) already tracked in open Linear issues:"
        )
        for f in already_tracked:
            issue = covered[f["id"]]
            write_summary(
                f">   - `{f['id']}` → [{issue['identifier']}]({issue['url']})"
            )

    if not new_findings:
        write_summary(
            f"> ✅ All {len(today_ids)} finding(s) already tracked — "
            "no new Linear issue created."
        )
        return 0

    # Partition new findings by severity → one ticket per non-empty bucket.
    buckets: dict[str, list[dict]] = {}
    for f in new_findings:
        buckets.setdefault(f["severity"], []).append(f)
    ordered_severities = sorted(buckets, key=lambda s: SEVERITY_RANK.get(s, 99))

    write_summary(
        f"> ⚠️ {len(new_findings)} new finding(s) across "
        f"{len(ordered_severities)} severit{'y' if len(ordered_severities) == 1 else 'ies'} "
        "— creating one Linear issue per severity."
    )

    backlog_state_id = resolve_backlog_state_id(team_id)
    if not backlog_state_id:
        print("warning: could not resolve Backlog state — issues will use team default")

    target_image = os.environ.get("TARGET_IMAGE", "(unknown)")
    scan_date = os.environ.get("SCAN_DATE", "")
    run_url = os.environ.get("RUN_URL", "")

    created: list[dict] = []
    for severity in ordered_severities:
        issue = create_issue(
            severity,
            buckets[severity],
            team_id=team_id,
            project_id=project_id,
            label_id=label_id,
            backlog_state_id=backlog_state_id,
            target_image=target_image,
            scan_date=scan_date,
            run_url=run_url,
        )
        if issue is None:
            return 1
        created.append(issue)

    emit_output("new_issues", json.dumps(created))
    return 0


if __name__ == "__main__":
    sys.exit(main())

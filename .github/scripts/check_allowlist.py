#!/usr/bin/env python3
"""Allowlist gate: check Trivy scan results against base-allowlist.json.

Usage:
    python3 check_allowlist.py --trivy-results <path> [--write-comment]

Environment:
    FAIL_ON_FINDINGS  "true" (default) to exit 1 on blockers; "false" for warning only.
    BUILD_RESULT      Result of the upstream image-build job, for diagnosing a
                      missing results file. Optional.
    SCAN_RESULT       Result of the upstream scan job, same purpose. Optional.
"""

import argparse
import json
import os
import sys
from datetime import datetime


def missing_results_reason(build_result: str, scan_result: str) -> str:
    """Name the upstream job that actually caused a missing results file.

    This gate is the required check, so its log is where anyone debugging a red
    PR lands first — and on its own it can only report that the file is absent,
    which reads as a scanner problem no matter what really broke. Three
    different transient failures (a build-cache write, a deleted PR merge ref,
    an artifact-service 403) have all surfaced here as the same line. So the
    upstream outcomes are passed in, and the cause is named.
    """
    if not build_result and not scan_result:
        return "Cause: unknown — no upstream job results were passed to this check."
    if build_result not in ("", "success", "skipped"):
        return (
            f"Cause: the image build did not succeed (result: {build_result}), so "
            "nothing was ever scanned. Read that job's log, not this one."
        )
    if scan_result not in ("", "success"):
        return (
            f"Cause: the scan job did not succeed (result: {scan_result}). Read "
            "that job's log, not this one."
        )
    return (
        "Cause: every upstream job succeeded, so the results were produced and "
        "the download of them failed twice. That is the artifact service, not "
        "this change — re-run this job."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trivy-results", required=True, help="Path to Trivy JSON output"
    )
    parser.add_argument(
        "--write-comment",
        action="store_true",
        default=False,
        help="Write summary to /tmp/scan_comment.md for PR comment posting",
    )
    args = parser.parse_args()

    fail_on_findings = os.environ.get("FAIL_ON_FINDINGS", "true").lower() == "true"

    base_path = "_sdk/.security/base-allowlist.json"
    try:
        with open(base_path) as f:
            base_raw = json.load(f)
        allowlist = {k: v for k, v in base_raw.items() if not k.startswith("_")}
        print(f"Base allowlist: {len(allowlist)} entries")
    except FileNotFoundError:
        print(
            f"Warning: {base_path} not found \u2014 findings reported but gate disabled."
        )
        allowlist = None
    except json.JSONDecodeError as e:
        print(f"Error: base allowlist has invalid JSON: {e}")
        sys.exit(1)

    today = datetime.now().strftime("%Y-%m-%d")
    all_vulns: dict = {}

    # Parse Trivy results
    trivy_count = 0
    try:
        with open(args.trivy_results) as f:
            trivy = json.load(f)
        for result in trivy.get("Results", []):
            for vuln in result.get("Vulnerabilities", []):
                vid = vuln.get("VulnerabilityID")
                if vid and vid not in all_vulns:
                    all_vulns[vid] = {
                        "id": vid,
                        "severity": vuln.get("Severity", "").upper(),
                        "package": vuln.get("PkgName", ""),
                        "installed": vuln.get("InstalledVersion", ""),
                        "fixed": vuln.get("FixedVersion", ""),
                        "source": "trivy",
                    }
                    trivy_count += 1
    except FileNotFoundError:
        print(f"Error: Trivy results not found at {args.trivy_results}")
        print(
            missing_results_reason(
                os.environ.get("BUILD_RESULT", ""),
                os.environ.get("SCAN_RESULT", ""),
            )
        )
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Trivy results have invalid JSON: {e}")
        sys.exit(1)

    unique = list(all_vulns.values())
    scanner_summary = f"Trivy: {trivy_count}"

    if allowlist is None:
        summary = (
            f"### \U0001f6e1\ufe0f Security Gate\n\n"
            f"Base allowlist unavailable \u2014 {len(unique)} CRITICAL/HIGH findings reported (gate disabled).\n"
        )
        for v in sorted(
            unique, key=lambda x: {"CRITICAL": 0, "HIGH": 1}.get(x["severity"], 9)
        ):
            print(
                f"  {v['severity']:8s} [{v['source']:5s}] {v['id']} {v['package']}@{v['installed']}"
            )
        _write_summary(summary, args.write_comment)
        sys.exit(0)

    allowed, expired, new_vulns = [], [], []
    for v in unique:
        entry = allowlist.get(v["id"])
        if entry:
            if today > entry.get("expires", "9999-12-31"):
                expired.append(v)
            else:
                allowed.append(v)
        else:
            new_vulns.append(v)

    sev_order = {"CRITICAL": 0, "HIGH": 1}
    new_vulns.sort(key=lambda x: sev_order.get(x["severity"], 9))
    expired.sort(key=lambda x: sev_order.get(x["severity"], 9))

    print(
        f"{scanner_summary} | Total: {len(unique)} | "
        f"Allowlisted: {len(allowed)} | Expired: {len(expired)} | New: {len(new_vulns)}"
    )

    blockers = new_vulns + expired
    if blockers:
        status = "Failed" if fail_on_findings else "Warning"
        summary = f"### \U0001f6e1\ufe0f Security Gate {status}\n\n"
        summary += (
            f"{scanner_summary} | Total: {len(unique)} | "
            f"Allowlisted: {len(allowed)} | **New: {len(new_vulns)}**"
        )
        if expired:
            summary += f" | Expired: {len(expired)}"
        summary += "\n\n"
        summary += "| Severity | Scanner | ID | Package | Fix Available |\n"
        summary += "|----------|---------|-----|---------|---------------|\n"
        for v in blockers:
            fix = v["fixed"] if v["fixed"] else "No fix"
            summary += f"| {v['severity']} | {v['source']} | `{v['id']}` | `{v['package']}@{v['installed']}` | {fix} |\n"
        summary += (
            "\n**To resolve:** fix the vulnerability or open a PR against "
            "`atlanhq/application-sdk` to add it to `.security/base-allowlist.json` "
            "(SDK security owner approval required).\n"
        )
        _write_summary(summary, args.write_comment)
        if fail_on_findings:
            print(f"GATE FAILED: {len(blockers)} non-allowlisted vulnerabilities")
            sys.exit(1)
        else:
            print(
                f"GATE WARNING: {len(blockers)} non-allowlisted vulnerabilities (non-blocking)"
            )
    else:
        summary = (
            f"### \U0001f6e1\ufe0f Security Gate Passed\n\n"
            f"{scanner_summary} | Total: {len(unique)} CRITICAL/HIGH | All allowlisted \u2705\n"
        )
        _write_summary(summary, args.write_comment)
        print(f"GATE PASSED: All {len(unique)} vulnerabilities are allowlisted")


def _write_summary(text: str, write_comment: bool) -> None:
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
        f.write(text)
    if write_comment:
        with open("/tmp/scan_comment.md", "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()

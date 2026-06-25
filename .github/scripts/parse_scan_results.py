#!/usr/bin/env python3
"""Parse the hourly Trivy scan results: counts, step output, workflow summary.

Replaces the inline shell that counted findings and built the GitHub step
summary — that branching/looping logic now lives here (tested) per
docs/standards/ci.md.

Reads ``trivy-image-results.json`` and ``trivy-fs-results.json`` from the cwd
(either may be absent) and:
  * emits ``total_actionable`` to ``$GITHUB_OUTPUT`` (the Linear-ticket step
    gates on it);
  * writes the human-readable vulnerability summary to ``$GITHUB_STEP_SUMMARY``.

Environment:
    GITHUB_OUTPUT         step output is appended here (optional locally)
    GITHUB_STEP_SUMMARY   markdown summary is appended here (optional locally)
    TARGET_IMAGE          shown in the summary header
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
IMAGE_FILE = "trivy-image-results.json"
FS_FILE = "trivy-fs-results.json"


def count_by_severity(data: dict | None) -> dict[str, int]:
    """Count vulnerabilities per severity in a parsed Trivy document."""
    counts = dict.fromkeys(SEVERITIES, 0)
    if not data:
        return counts
    for result in data.get("Results", []) or []:
        for vuln in result.get("Vulnerabilities", []) or []:
            sev = (vuln.get("Severity") or "").upper()
            if sev in counts:
                counts[sev] += 1
    return counts


def load(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        print(f"warning: {path} is not valid JSON; treating as empty")
        return None


def build_summary(image: dict[str, int], fs: dict[str, int], target_image: str) -> str:
    total = {s: image[s] + fs[s] for s in SEVERITIES}
    total_actionable = sum(total.values())
    emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}
    label = {"CRITICAL": "Critical", "HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}
    lines = [
        f"## Hourly Security Scan — `{target_image}` + SDK Python Deps",
        "",
        "### Vulnerability Summary",
        "",
        "| Severity | Trivy (Image) | Trivy (Deps) | Total |",
        "|----------|:-------------:|:--------------:|------:|",
    ]
    for s in SEVERITIES:
        lines.append(
            f"| {emoji[s]} {label[s]} | {image[s]} | {fs[s]} | **{total[s]}** |"
        )
    lines.append("")
    if total_actionable > 0:
        lines.append(
            f"> ⚠️ **{total_actionable} findings (all severities) — "
            "see Linear ticket step below**"
        )
    else:
        lines.append("> ✅ **No findings**")
    return "\n".join(lines)


def _emit_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{name}={value}\n")


def _emit_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


def main() -> int:
    image = count_by_severity(load(IMAGE_FILE))
    fs = count_by_severity(load(FS_FILE))
    total_actionable = sum(image[s] + fs[s] for s in SEVERITIES)

    _emit_output("total_actionable", str(total_actionable))
    _emit_summary(build_summary(image, fs, os.environ.get("TARGET_IMAGE", "(unknown)")))

    print(
        f"Trivy Image  — C:{image['CRITICAL']} H:{image['HIGH']} "
        f"M:{image['MEDIUM']} L:{image['LOW']}"
    )
    print(
        f"Trivy Python — C:{fs['CRITICAL']} H:{fs['HIGH']} "
        f"M:{fs['MEDIUM']} L:{fs['LOW']}"
    )
    print(f"Total findings (all severities): {total_actionable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

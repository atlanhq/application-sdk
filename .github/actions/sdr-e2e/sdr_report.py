"""Render the sdr-e2e run's reports from pytest + container output.

Each subcommand used to be an inline ``python3 - <<'PYEOF'`` heredoc in
``action.yaml``; docs/standards/ci.md keeps branching logic out of inlined
shell, so it lives here, beside the action (see ``sdr_steps.py`` for why).
Tests: ``.github/scripts/tests/test_sdr_e2e_report.py``.

All paths are relative to the working directory (the caller's workspace).

* ``workflow-links``  Temporal history links per scenario → ``links`` output.
* ``step-summary``    pytest totals + per-scenario table → step summary.
* ``asset-summary``   extract/transform counts from the container log, plus
                      the full-DAG Atlas sections → ``markdown`` output.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import secrets
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from itertools import groupby
from pathlib import Path

JUNIT_XML = "results/sdr-test-results.xml"
TEST_OUTPUT = "results/sdr-test-output.txt"
CONTAINER_LOG = "results/sdr-container.log"


def _write_output(name: str, value: str, delim_prefix: str) -> None:
    # Multi-line outputs need the heredoc syntax.
    delim = delim_prefix + secrets.token_hex(8)
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def _temporal_url(tenant: str, wf: str, run: str) -> str:
    # The Atlan Temporal proxy rejects /workflows/<wf>/history without run_id.
    return (
        f"https://{tenant}/api/temporal/namespaces/default/workflows/{wf}/{run}/history"
    )


# --------------------------------------------------------------------------
# workflow-links
# --------------------------------------------------------------------------


def _class_by_scenario() -> dict[str, str]:
    """Scenario name → pytest test class, to group links for multi-class apps."""
    class_by_scenario: dict[str, str] = {}
    try:
        tree = ET.parse(JUNIT_XML)
        for tc in tree.getroot().iter("testcase"):
            classname = (tc.get("classname") or "").rsplit(".", 1)[-1]
            scenario = (tc.get("name") or "").removeprefix("test_")
            if classname and scenario:
                # Keeps one class per name, so it is only the fallback for the
                # shared summary file; a per-class file names its own class
                # (_summary_class), which is what keeps two classes' same-named
                # scenarios apart.
                class_by_scenario[scenario] = classname
    except Exception as exc:  # JUnit XML missing → fall through to ungrouped list
        print(f"No JUnit results to group links by ({type(exc).__name__}).")
    return class_by_scenario


def _summary_class(summary_file: Path) -> str:
    """Class named by a per-class summary file, "" for the shared one.

    application_sdk.testing.sdr.base writes
    ``integration-test-summary-<cls.__name__>.json`` per class.
    """
    if summary_file.stem == "integration-test-summary":
        return ""
    return summary_file.stem.removeprefix("integration-test-summary-")


def render_workflow_links(tenant: str) -> str:
    class_by_scenario = _class_by_scenario()
    icons = {"passed": "✅", "failed": "❌", "skipped": "⏭"}

    # Multi-class apps land per-class summaries via the SDR base class;
    # single-class apps still write only the shared file. Read all that exist.
    per_class = sorted(Path(".").glob("integration-test-summary-*.json"))
    if per_class:
        summary_files = per_class
    elif Path("integration-test-summary.json").exists():
        summary_files = [Path("integration-test-summary.json")]
    else:
        summary_files = []

    # Scenario insertion order (== test execution order) within each class.
    by_class: dict[str, list[str]] = {}
    for summary_file in summary_files:
        if not tenant:
            break
        file_class = _summary_class(summary_file)
        data = json.loads(summary_file.read_text())
        for s in data.get("scenarios", []):
            if s.get("api") != "workflow":
                continue
            assertions = s.get("assertions") or {}
            wf = assertions.get("data.workflow_id", {}).get("actual", "")
            run = assertions.get("data.run_id", {}).get("actual", "")
            # No workflow_id means /workflow/start failed before returning IDs.
            if not (wf and run):
                continue
            icon = icons.get(s.get("status", ""), "❓")
            cls = file_class or class_by_scenario.get(s["name"], "")
            by_class.setdefault(cls, []).append(
                f"{icon} [{s['name']}]({_temporal_url(tenant, wf, run)})"
            )

    # Single suite OR no class info → flat list separated by ` · `.
    # Multiple suites → bulleted list grouped by class on its own line.
    if not by_class:
        return ""
    if len(by_class) == 1:
        return " · ".join(next(iter(by_class.values())))
    lines = [""]
    for cls, items in by_class.items():
        label = f"**{cls}:**" if cls else "**(unknown suite):**"
        lines.append(f"  - {label} {' · '.join(items)}")
    return "\n".join(lines)


def workflow_links() -> int:
    out = render_workflow_links(os.environ.get("SDR_TEST_TENANT", ""))
    _write_output("links", out, "EOF_LINKS_")
    print(f"Temporal links:\n{out or '(none)'}")
    return 0


# --------------------------------------------------------------------------
# step-summary
# --------------------------------------------------------------------------


def title_from_test_path(path: str) -> str:
    """Self-labelling report title; mirrored by the PR-comment step's JS."""
    specials = {
        "sdr": "SDR Integration Tests (testcontainer)",
        "full_dag": "E2E Full Tests (system apps)",
    }
    seg = path.strip("/").split("/")[-1] if path else ""
    if seg in specials:
        return specials[seg]
    if not seg:
        return "Tests"
    return seg.replace("-", " ").replace("_", " ").title() + " Tests"


def render_step_summary(
    outcome: str, app_name: str, report_title: str, summary_path: str
) -> None:
    icon = "✅" if outcome == "success" else "❌"
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    scenarios: list[tuple[str, str, str, float, str]] = []

    try:
        tree = ET.parse(JUNIT_XML)
        for suite in tree.getroot().iter("testsuite"):
            totals["tests"] += int(suite.get("tests", 0))
            totals["failures"] += int(suite.get("failures", 0))
            totals["errors"] += int(suite.get("errors", 0))
            totals["skipped"] += int(suite.get("skipped", 0))
            totals["time"] += float(suite.get("time", 0))
        for tc in tree.getroot().iter("testcase"):
            raw_name = tc.get("name", "")
            t = float(tc.get("time", 0))
            short = raw_name.removeprefix("test_")
            group = short.split("_")[0]
            if tc.find("skipped") is not None:
                status = "⏭"
                msg = (tc.find("skipped").get("message") or "")[:80]
            elif tc.find("failure") is not None:
                status = "❌"
                msg = (tc.find("failure").get("message") or "")[:80]
            elif tc.find("error") is not None:
                status = "💥"
                msg = (tc.find("error").get("message") or "")[:80]
            else:
                status = "✅"
                msg = ""
            scenarios.append((group, short, status, t, msg))
    except Exception as exc:  # no JUnit XML → summary without the tables
        print(f"No JUnit results for the step summary ({type(exc).__name__}).")

    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]

    raw_summary = ""
    try:
        with open(TEST_OUTPUT) as f:
            for line in f:
                if re.match(r"^=+", line) and (
                    "passed" in line or "failed" in line or "error" in line
                ):
                    raw_summary = line.strip()
    except Exception as exc:  # no pytest output → summary without that line
        print(f"No pytest output for the step summary ({type(exc).__name__}).")

    with open(summary_path, "w") as f:
        f.write(f"## {icon} {report_title} — {app_name}\n\n")

        if totals["tests"]:
            f.write("### Test results\n\n")
            f.write("| Result | Count |\n|--------|-------|\n")
            f.write(f"| ✅ Passed  | {passed} |\n")
            f.write(f"| ❌ Failed  | {totals['failures'] + totals['errors']} |\n")
            f.write(f"| ⏭ Skipped | {totals['skipped']} |\n")
            f.write(f"| ⏱ Total time | {totals['time']:.1f}s |\n\n")

        if raw_summary:
            f.write(f"```\n{raw_summary}\n```\n\n")

        if scenarios:
            f.write("### Scenario details\n\n")
            for group, group_scenarios in groupby(scenarios, key=lambda s: s[0]):
                rows = list(group_scenarios)
                f.write(
                    f"<details>\n<summary><b>{group.capitalize()}</b> "
                    f"({sum(1 for r in rows if r[2] == '✅')} passed, "
                    f"{sum(1 for r in rows if r[2] in ('❌', '💥'))} failed, "
                    f"{sum(1 for r in rows if r[2] == '⏭')} skipped)</summary>\n\n"
                )
                f.write("| Status | Scenario | Time |\n|--------|----------|------|\n")
                for _, short, status, t, msg in rows:
                    time_str = f"{t:.1f}s" if t >= 0.1 else "<0.1s"
                    note = f" — _{msg}_" if msg else ""
                    f.write(f"| {status} | `{short}`{note} | {time_str} |\n")
                f.write("\n</details>\n\n")


def step_summary() -> int:
    report_title = os.environ.get("REPORT_TITLE_OVERRIDE") or title_from_test_path(
        os.environ.get("TEST_PATH", "")
    )
    render_step_summary(
        os.environ.get("OUTCOME", "unknown"),
        os.environ.get("APP_NAME", "app"),
        report_title,
        os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout"),
    )
    return 0


# --------------------------------------------------------------------------
# asset-summary
# --------------------------------------------------------------------------

# correlation_id=<uuid>  ...  Extracted <entity>: N raw records
_EXTRACT_RE = re.compile(
    r"correlation_id=(?P<cid>[0-9a-f-]+)[^\n]*?"
    r"Extracted (?P<entity>[A-Za-z0-9_-]+): (?P<n>\d+) raw records"
)
_TRANSFORM_RE = re.compile(
    r"correlation_id=(?P<cid>[0-9a-f-]+)[^\n]*?"
    r"Transformed (?P<entity>[A-Za-z0-9_-]+): (?P<n>\d+) records"
)
# workflow_id + run_id emitted under the same correlation block; only link
# when both are present (see _temporal_url).
_WF_RE = re.compile(
    r"correlation_id=(?P<cid>[0-9a-f-]+)[^\n]*?workflow_id=(?P<wf>[\w-]+)"
)
_RUN_RE = re.compile(r"correlation_id=(?P<cid>[0-9a-f-]+)[^\n]*?run_id=(?P<run>[\w-]+)")
_ASSETS_RE = re.compile(r"Atlas inventory under (?P<qn>\S+):\s+(?P<counts>\{.*?\})")
_LINEAGE_RE = re.compile(
    r"Lineage inventory under (?P<qn>\S+):\s+(?P<counts>\{.*?\})"
    r"\s+lineage_present=(?P<lin>True|False)"
)


def _safe_dict(literal: str) -> dict[str, int]:
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return {}
    return value if isinstance(value, dict) else {}


def _render_section(title: str, qn: str, counts: dict[str, int]) -> list[str]:
    total = sum(counts.values())
    section = [
        f"### {title}",
        "",
        f"**Connection:** `{qn}`",
        "",
        f"**Total:** {total}",
        "",
    ]
    if counts:
        section.append("| Type | Count |")
        section.append("|------|-------|")
        for type_name in sorted(counts):
            section.append(f"| {type_name} | {counts[type_name]} |")
        section.append("")
    return section


def render_asset_summary(text: str, tenant: str, two_store: bool) -> str | None:
    """The markdown block, or None when the log has no extract/transform lines."""
    cids: list[str] = []  # insertion-ordered for deterministic table order
    extracted: dict[str, dict[str, int]] = defaultdict(dict)
    transformed: dict[str, dict[str, int]] = defaultdict(dict)
    wf_id_by_cid: dict[str, str] = {}
    run_id_by_cid: dict[str, str] = {}

    def _seen(cid: str) -> None:
        if cid not in cids:
            cids.append(cid)

    for m in _EXTRACT_RE.finditer(text):
        _seen(m.group("cid"))
        extracted[m.group("cid")][m.group("entity")] = int(m.group("n"))
    for m in _TRANSFORM_RE.finditer(text):
        _seen(m.group("cid"))
        transformed[m.group("cid")][m.group("entity")] = int(m.group("n"))
    for m in _WF_RE.finditer(text):
        wf_id_by_cid.setdefault(m.group("cid"), m.group("wf"))
    for m in _RUN_RE.finditer(text):
        run_id_by_cid.setdefault(m.group("cid"), m.group("run"))

    # Drop correlation groups that produced no asset rows (handler-level auth /
    # preflight scenarios never emit extract/transform lines).
    cids = [c for c in cids if extracted.get(c) or transformed.get(c)]
    if not cids:
        return None

    lines: list[str] = ["### Extracted + transformed assets", ""]
    for idx, cid in enumerate(cids, start=1):
        wf = wf_id_by_cid.get(cid, "")
        run = run_id_by_cid.get(cid, "")
        header = f"**Workflow run {idx}**"
        if wf and run and tenant:
            header += f" — [{wf}]({_temporal_url(tenant, wf, run)})"
        elif wf:
            # No run id: show the id verbatim but no link (the proxy 404s).
            header += f" — `{wf}`"
        lines.append(header)
        lines.append("")
        lines.append("| Entity | Extracted | Transformed |")
        lines.append("|--------|-----------|-------------|")
        for entity in sorted(set(extracted[cid]) | set(transformed[cid])):
            ext = extracted[cid].get(entity)
            tr = transformed[cid].get(entity)
            ext_s = "—" if ext is None else str(ext)
            tr_s = "—" if tr is None else str(tr)
            lines.append(f"| {entity} | {ext_s} | {tr_s} |")
        lines.append("")

    # Full-DAG runs append two Atlas-side sections mirroring the workflow-center
    # UI cards. SDR runs never emit these lines, so they appear only on a match.
    test_log = Path(TEST_OUTPUT)
    if test_log.exists():
        test_text = test_log.read_text(errors="replace")
        # Take the last match in case retries logged earlier ones.
        assets_match = next(iter(list(_ASSETS_RE.finditer(test_text))[::-1]), None)
        lineage_match = next(iter(list(_LINEAGE_RE.finditer(test_text))[::-1]), None)
        if assets_match:
            atlas_counts = _safe_dict(assets_match.group("counts"))
            lines.extend(
                _render_section(
                    "Assets in Atlas", assets_match.group("qn"), atlas_counts
                )
            )
            # Two-store diagnostic, not a gate: extraction succeeding locally
            # while nothing reaches Atlas is the ADR-0014 hand-off gap.
            if two_store:
                total_transformed = sum(
                    n
                    for entity_counts in transformed.values()
                    for n in entity_counts.values()
                )
                if total_transformed > 0 and sum(atlas_counts.values()) == 0:
                    lines.append(
                        "> ⚠️ **Possible missing hand-off:** the connector "
                        f"transformed {total_transformed} record(s) locally "
                        "but 0 assets reached Atlas. Check for a missing "
                        "`App.upload()` bridging the transformed-data prefix "
                        "from the deployment `objectstore` to "
                        "`atlan-objectstore` (see ADR-0014)."
                    )
                    lines.append("")
        if lineage_match:
            lineage_counts = _safe_dict(lineage_match.group("counts"))
            lines.extend(
                _render_section(
                    "Lineage coverage", lineage_match.group("qn"), lineage_counts
                )
            )
            if (
                lineage_match.group("lin") == "False"
                and sum(lineage_counts.values()) == 0
            ):
                lines.append(
                    "> No lineage assets observed — waived when the seed dataset "
                    "has no view-derived lineage."
                )
                lines.append("")

    return "\n".join(lines)


def asset_summary() -> int:
    log_path = Path(CONTAINER_LOG)
    if not log_path.exists():
        print("No container log; skipping asset summary.")
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("markdown=\n")
        return 0
    markdown = render_asset_summary(
        log_path.read_text(errors="replace"),
        os.environ.get("SDR_TEST_TENANT", ""),
        os.environ.get("TWO_STORE") == "true",
    )
    if markdown is None:
        print("No extract/transform lines in container log.")
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("markdown=\n")
        return 0
    _write_output("markdown", markdown, "EOF_ASSETS_")
    print(markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("workflow-links", "step-summary", "asset-summary")
    )
    args = parser.parse_args(argv)
    if args.command == "workflow-links":
        return workflow_links()
    if args.command == "step-summary":
        return step_summary()
    return asset_summary()


if __name__ == "__main__":
    sys.exit(main())

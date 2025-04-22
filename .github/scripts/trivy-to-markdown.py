import argparse
import json
from enum import Enum
from typing import Any, Dict

from mdutils.mdutils import MdUtils


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


def convert_trivy_to_markdown(
    trivy_data: Dict[str, Any],
    report_type: str,
) -> str:
    """Convert Trivy scan data to Markdown format.

    Args:
        trivy_data: The Trivy scan data as a dictionary
        report_type: The type of report (e.g., "Vulnerability", "Secret")

    Returns:
        str: The markdown content
    """
    md_file = MdUtils(file_name="result.md")

    md_file.write(f"## 📦 Trivy {report_type} Scan Results\n\n")

    md_file.new_table(
        columns=4,
        rows=2,
        text=[
            "Schema Version",
            "Created At",
            "Artifact",
            "Type",
            trivy_data.get("SchemaVersion", "Unknown"),
            trivy_data.get("CreatedAt", "Unknown"),
            trivy_data.get("ArtifactName", "Unknown"),
            trivy_data.get("ArtifactType", "Unknown"),
        ],
    )
    md_file.new_line()

    md_file.write("### Report Summary\n\n")
    summary_table_items = ["Target", "Type"]
    is_vuln_report = "vulnerability" in report_type.lower()
    is_secret_report = "secret" in report_type.lower()

    if is_vuln_report:
        summary_table_items.append("Vulnerabilities")
    if is_secret_report:
        summary_table_items.append("Secrets")

    results_data = trivy_data.get("Results") or []
    artifact_name = trivy_data.get("ArtifactName", "Unknown Artifact")
    artifact_type = trivy_data.get("ArtifactType", "Unknown Type")

    if results_data is None:
        if is_vuln_report and is_secret_report:
            summary_table_items.extend(["No targets scanned", "-", "-", "-"])
        elif is_vuln_report:
            summary_table_items.extend(
                [f"`{artifact_name}`", artifact_type, "Scan Error?"]
            )
        elif is_secret_report:
            summary_table_items.extend(
                [f"`{artifact_name}`", artifact_type, "Scan Error?"]
            )
        else:
            summary_table_items.extend(["No targets scanned", "-"])
        results_list = []

    elif isinstance(results_data, list) and not results_data:
        target_display = f"`{artifact_name}`"
        row_data = [target_display, artifact_type]
        if is_vuln_report:
            row_data.append("✅ None found")
        if is_secret_report:
            row_data.append("✅ None found")
        summary_table_items.extend(row_data)
        results_list = []

    elif isinstance(results_data, list):
        results_list = results_data
        for result in results_list:
            target = f"`{result.get('Target', 'Unknown Target')}`"
            result_type = result.get("Type", "Unknown Type")
            row_data = [target, result_type]

            if is_vuln_report:
                severity_counts = {}
                vulnerabilities = result.get("Vulnerabilities", [])
                for vuln in vulnerabilities:
                    severity = vuln.get("Severity", "UNKNOWN")
                    severity_counts[severity] = severity_counts.get(severity, 0) + 1
                severity_order = {
                    "CRITICAL": 0,
                    "HIGH": 1,
                    "MEDIUM": 2,
                    "LOW": 3,
                    "UNKNOWN": 4,
                }
                vuln_count = sum(severity_counts.values())
                if vuln_count > 0:
                    severity_breakdown = ", ".join(
                        f"{count} {sev.capitalize()}"
                        for sev, count in sorted(
                            severity_counts.items(),
                            key=lambda x: severity_order.get(x[0], 5),
                        )
                    )
                    vuln_str = f"**{vuln_count}** ({severity_breakdown})"
                else:
                    vuln_str = "✅ None found"
                row_data.append(vuln_str)

            if is_secret_report:
                secrets = result.get("Secrets", [])
                secret_count = len(secrets)
                secret_str = (
                    f"**{secret_count}**" if secret_count > 0 else "✅ None found"
                )
                row_data.append(secret_str)

            summary_table_items.extend(row_data)
    else:
        summary_table_items.extend(
            [f"`{artifact_name}`", artifact_type, "Result Error"]
        )
        results_list = []

    num_targets_processed = len(results_list) if results_list else 1
    num_columns = (
        len(summary_table_items) // num_targets_processed
        if num_targets_processed > 0
        else 0
    )
    num_rows = num_targets_processed

    if num_columns > 0 and num_rows > 0:
        expected_length = num_columns * num_rows
        if len(summary_table_items) == expected_length:
            md_file.new_table(
                columns=num_columns,
                rows=num_rows,
                text=summary_table_items,
                text_align="left",
            )
        else:
            md_file.write(
                f"Could not generate summary table (data length mismatch: {len(summary_table_items)} vs {expected_length}).\n\n"
            )
    else:
        md_file.write("Could not generate summary table.\n\n")

    md_file.new_line()
    md_file.new_line()
    md_file.write("### Scan Result Details\n\n")

    if results_data is None:
        md_file.write(
            "No scan results found in the input data (Results key missing or null).\n\n"
        )
    elif isinstance(results_data, list) and not results_data:
        scan_type_name = (
            "vulnerabilities"
            if is_vuln_report
            else "secrets"
            if is_secret_report
            else "items"
        )
        md_file.write(
            f"✅ No {scan_type_name} found during the scan for `{artifact_name}`.\n\n"
        )
    elif isinstance(results_data, list):
        for result in results_list:
            target = result.get("Target", "Unknown Target")
            md_file.write(f"<details>\n<summary>{target}</summary>\n\n")

            vulnerabilities = result.get("Vulnerabilities")
            if vulnerabilities is not None:
                md_file.write("#### Vulnerabilities\n\n")
                if not vulnerabilities:
                    md_file.write("No vulnerabilities found for this target.\n\n")
                else:
                    vulnerabilities_table_items = [
                        "Severity",
                        "ID",
                        "Package",
                        "Version",
                        "Fixed Version",
                        "Title",
                    ]
                    for vuln in vulnerabilities:
                        severity = vuln.get("Severity", "UNKNOWN")
                        severity_enum = (
                            Severity(severity)
                            if severity in Severity.__members__
                            else Severity.UNKNOWN
                        )
                        severity_emoji = {
                            Severity.CRITICAL: "🔴",
                            Severity.HIGH: "🟠",
                            Severity.MEDIUM: "🟡",
                            Severity.LOW: "🟢",
                            Severity.UNKNOWN: "⚪",
                        }.get(severity_enum, "⚪")

                        vulnerabilities_table_items.extend(
                            [
                                f"{severity_emoji} {severity}",
                                f"[{vuln.get('VulnerabilityID', 'N/A')}]({vuln.get('PrimaryURL', '#')})",
                                vuln.get("PkgName", "N/A"),
                                vuln.get("InstalledVersion", "N/A"),
                                vuln.get("FixedVersion", "N/A"),
                                vuln.get("Title", "N/A"),
                            ]
                        )

                    if len(vulnerabilities_table_items) > 6:
                        md_file.new_table(
                            columns=6,
                            rows=len(vulnerabilities_table_items) // 6,
                            text=vulnerabilities_table_items,
                            text_align="left",
                        )
                        md_file.new_line()

            secrets = result.get("Secrets")
            if secrets is not None:
                md_file.write("#### Secrets\n\n")
                if not secrets:
                    md_file.write("No secrets found for this target.\n\n")
                else:
                    secrets_table_items = [
                        "Severity",
                        "Rule ID",
                        "Category",
                        "Title",
                        "Location",
                    ]
                    for secret in secrets:
                        severity = secret.get("Severity", "UNKNOWN")
                        severity_enum = (
                            Severity(severity)
                            if severity in Severity.__members__
                            else Severity.UNKNOWN
                        )
                        severity_emoji = {
                            Severity.CRITICAL: "🔴",
                            Severity.HIGH: "🟠",
                            Severity.MEDIUM: "🟡",
                            Severity.LOW: "🟢",
                            Severity.UNKNOWN: "⚪",
                        }.get(severity_enum, "⚪")

                        location = f"Line {secret.get('StartLine', 'N/A')}"
                        if secret.get("EndLine") and secret.get(
                            "EndLine"
                        ) != secret.get("StartLine"):
                            location += f"-{secret.get('EndLine')}"

                        secrets_table_items.extend(
                            [
                                f"{severity_emoji} {severity}",
                                secret.get("RuleID", "N/A"),
                                secret.get("Category", "N/A"),
                                secret.get("Title", "N/A"),
                                location,
                            ]
                        )

                    if len(secrets_table_items) > 5:
                        md_file.new_table(
                            columns=5,
                            rows=len(secrets_table_items) // 5,
                            text=secrets_table_items,
                            text_align="left",
                        )
                        md_file.new_line()

            md_file.write("</details>\n\n")
    else:
        md_file.write(
            "Error processing scan results: Unexpected data format for 'Results'.\n\n"
        )

    return md_file.get_md_text()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert Trivy scan results to Markdown"
    )
    parser.add_argument("input_file", help="Input Trivy JSON file")
    parser.add_argument("output_file", help="Output Markdown file")

    parser.add_argument(
        "report_type", help="Type of report (e.g., Vulnerability, Secret)"
    )
    args = parser.parse_args()

    trivy_data = json.load(open(args.input_file))
    md_text = convert_trivy_to_markdown(trivy_data, args.report_type)

    with open(args.output_file, "w") as f:
        f.write(md_text)

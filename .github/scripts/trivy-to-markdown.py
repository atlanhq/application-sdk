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
) -> str:
    """Convert Trivy scan data to Markdown format.

    Args:
        trivy_data: The Trivy scan data as a dictionary
        title: The title for the markdown output

    Returns:
        str: The markdown content
    """
    md_file = MdUtils(file_name="result.md")

    md_file.write("## 📦 Trivy Scan Results\n\n")

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

    # Create summary table header
    md_file.write("### Report Summary\n\n")
    summary_table_items = ["Target", "Type", "Vulnerabilities", "Secrets"]

    # Group results by target and count vulnerabilities and secrets
    for result in trivy_data.get("Results", []):
        target = f"`{result.get('Target', 'Unknown Target')}`"
        result_type = result.get("Type", "Unknown Type")

        # Count vulnerabilities by severity
        severity_counts = {}
        for vuln in result.get("Vulnerabilities", []):
            severity = vuln.get("Severity", "UNKNOWN")
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

        # Sort severities by priority
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}

        # Format vulnerability count with severity breakdown
        vuln_count = sum(severity_counts.values())
        if vuln_count > 0:
            severity_breakdown = ", ".join(
                f"{count} {sev.capitalize()}"
                for sev, count in sorted(
                    severity_counts.items(), key=lambda x: severity_order[x[0]]
                )
            )
            vuln_str = f"**{vuln_count}** ({severity_breakdown})"
        else:
            vuln_str = "-"

        # Count secrets
        secret_count = len(result.get("Secrets", []))
        secret_str = str(secret_count) if secret_count > 0 else "-"
        if secret_count > 0:
            secret_str = f"**{secret_str}**"

        summary_table_items.extend([target, result_type, vuln_str, secret_str])

    md_file.new_table(
        columns=4,
        rows=len(summary_table_items) // 4,
        text=summary_table_items,
        text_align="left",
    )
    md_file.new_line()
    md_file.new_line()
    md_file.write("### Scan Result Details\n\n")

    # Process each result with details in collapsible sections
    for result in trivy_data.get("Results", []):
        target = result.get("Target", "Unknown Target")
        result_type = result.get("Type", "Unknown Type")

        # Start collapsible section for this target
        md_file.write(f"<details>\n<summary>{target}</summary>\n\n")

        # Process vulnerabilities
        if "Vulnerabilities" in result:
            md_file.write("#### Vulnerabilities\n\n")
            vulnerabilities_table_items = [
                "Severity",
                "ID",
                "Package",
                "Version",
                "Fixed Version",
                "Title",
            ]

            for vuln in result["Vulnerabilities"]:
                severity = vuln.get("Severity", "UNKNOWN")
                severity_emoji = {
                    Severity.CRITICAL: "🔴",
                    Severity.HIGH: "🟠",
                    Severity.MEDIUM: "🟡",
                    Severity.LOW: "🟢",
                    Severity.UNKNOWN: "⚪",
                }.get(Severity(severity), "⚪")

                # Add row to table
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

            # Create and add the table
            if len(vulnerabilities_table_items) > 6:
                md_file.new_table(
                    columns=6,
                    rows=len(vulnerabilities_table_items) // 6,
                    text=vulnerabilities_table_items,
                    text_align="left",
                )
                md_file.new_line()

        # Process secrets
        if "Secrets" in result:
            md_file.write("#### Secrets\n\n")
            secrets_table_items = [
                "Severity",
                "Rule ID",
                "Category",
                "Title",
                "Location",
            ]

            for secret in result["Secrets"]:
                severity = secret.get("Severity", "UNKNOWN")
                severity_emoji = {
                    Severity.CRITICAL: "🔴",
                    Severity.HIGH: "🟠",
                    Severity.MEDIUM: "🟡",
                    Severity.LOW: "🟢",
                    Severity.UNKNOWN: "⚪",
                }.get(Severity(severity), "⚪")

                # Get location information
                location = f"Line {secret.get('StartLine', 'N/A')}"
                if secret.get("EndLine") and secret.get("EndLine") != secret.get(
                    "StartLine"
                ):
                    location += f"-{secret.get('EndLine')}"

                # Add row to table
                secrets_table_items.extend(
                    [
                        f"{severity_emoji} {severity}",
                        secret.get("RuleID", "N/A"),
                        secret.get("Category", "N/A"),
                        secret.get("Title", "N/A"),
                        location,
                    ]
                )

            # Create and add the table
            if len(secrets_table_items) > 5:
                md_file.new_table(
                    columns=5,
                    rows=len(secrets_table_items) // 5,
                    text=secrets_table_items,
                    text_align="left",
                )
                md_file.new_line()

        # Close the collapsible section
        md_file.write("</details>\n\n")

    return md_file.get_md_text()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert Trivy scan results to Markdown"
    )
    parser.add_argument("input_file", help="Input Trivy JSON file")
    parser.add_argument("output_file", help="Output Markdown file")
    args = parser.parse_args()

    trivy_data = json.load(open(args.input_file))
    md_text = convert_trivy_to_markdown(trivy_data)
    with open(args.output_file, "w") as f:
        f.write(md_text)

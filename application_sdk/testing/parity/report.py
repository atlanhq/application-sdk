"""Report generation for parity comparison results."""

from typing import Any

from application_sdk.testing.parity.models import CategoryResult


def _format_value(val: Any) -> str:
    """Format a value for display, truncating long strings."""
    s = repr(val)
    if len(s) > 80:
        return s[:77] + "..."
    return s


def generate_markdown(
    results: list[CategoryResult],
    baseline_ref: str = "main",
    candidate_ref: str = "PR",
) -> str:
    """Generate a markdown report from comparison results."""
    has_any_diffs = any(r.has_diffs for r in results)
    total_added = sum(len(r.added) for r in results)
    total_removed = sum(len(r.removed) for r in results)
    total_modified = sum(len(r.modified) for r in results)

    lines: list[str] = []
    lines.append("## Parity Test Results\n")

    if has_any_diffs:
        lines.append(
            f"**Verdict: PARITY BROKEN** — "
            f"{total_added} added, {total_removed} removed, {total_modified} modified\n"
        )
    else:
        lines.append("**Verdict: PARITY** — no differences found\n")

    lines.append(f"**Baseline**: `{baseline_ref}` | **Candidate**: `{candidate_ref}`\n")

    # Summary table
    lines.append("### Summary\n")
    lines.append("| Category | Baseline | Candidate | Added | Removed | Modified |")
    lines.append("|----------|----------|-----------|-------|---------|----------|")
    for r in results:
        lines.append(
            f"| {r.category} | {r.baseline_count} | {r.candidate_count} "
            f"| {len(r.added)} | {len(r.removed)} | {len(r.modified)} |"
        )
    lines.append("")

    # Details per category (collapsible)
    for r in results:
        if not r.has_diffs:
            continue

        parts = []
        if r.added:
            parts.append(f"{len(r.added)} added")
        if r.removed:
            parts.append(f"{len(r.removed)} removed")
        if r.modified:
            parts.append(f"{len(r.modified)} modified")
        summary = ", ".join(parts)

        lines.append("<details>")
        lines.append(f"<summary><b>{r.category}</b>: {summary}</summary>\n")

        if r.added:
            lines.append("**Added:**")
            for a in r.added[:50]:
                lines.append(f"- `{a.qualified_name}` ({a.type_name})")
            if len(r.added) > 50:
                lines.append(f"- ... and {len(r.added) - 50} more")
            lines.append("")

        if r.removed:
            lines.append("**Removed:**")
            for a in r.removed[:50]:
                lines.append(f"- `{a.qualified_name}` ({a.type_name})")
            if len(r.removed) > 50:
                lines.append(f"- ... and {len(r.removed) - 50} more")
            lines.append("")

        if r.modified:
            lines.append("**Modified:**")
            for a in r.modified[:30]:
                lines.append(f"- `{a.qualified_name}` ({a.type_name}):")
                for fd in a.field_diffs[:10]:
                    b_str = _format_value(fd.baseline_value)
                    c_str = _format_value(fd.candidate_value)
                    lines.append(f"  - `{fd.field_path}`: {b_str} → {c_str}")
                if len(a.field_diffs) > 10:
                    lines.append(f"  - ... and {len(a.field_diffs) - 10} more fields")
            if len(r.modified) > 30:
                lines.append(f"- ... and {len(r.modified) - 30} more")
            lines.append("")

        lines.append("</details>\n")

    if has_any_diffs:
        lines.append("> Add label `parity-accepted` to acknowledge these changes.\n")

    return "\n".join(lines)


def generate_json_report(
    results: list[CategoryResult],
    baseline_ref: str = "main",
    candidate_ref: str = "PR",
) -> dict[str, Any]:
    """Generate a JSON report from comparison results."""
    has_any_diffs = any(r.has_diffs for r in results)
    return {
        "is_parity": not has_any_diffs,
        "baseline_ref": baseline_ref,
        "candidate_ref": candidate_ref,
        "summary": {
            "total_added": sum(len(r.added) for r in results),
            "total_removed": sum(len(r.removed) for r in results),
            "total_modified": sum(len(r.modified) for r in results),
        },
        "categories": [
            {
                "category": r.category,
                "baseline_count": r.baseline_count,
                "candidate_count": r.candidate_count,
                "added": [
                    {"qualified_name": a.qualified_name, "type_name": a.type_name}
                    for a in r.added
                ],
                "removed": [
                    {"qualified_name": a.qualified_name, "type_name": a.type_name}
                    for a in r.removed
                ],
                "modified": [
                    {
                        "qualified_name": a.qualified_name,
                        "type_name": a.type_name,
                        "field_diffs": [
                            {
                                "field": fd.field_path,
                                "baseline": fd.baseline_value,
                                "candidate": fd.candidate_value,
                            }
                            for fd in a.field_diffs
                        ],
                    }
                    for a in r.modified
                ],
            }
            for r in results
        ],
    }

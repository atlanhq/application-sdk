"""C001 UnpinnedActionReference — scan GitHub Actions workflow files for non-digest pins."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.schema.findings import Finding, findings_to_report

SERIES = "C"
RULE_ID = "C001"
EXEMPT_OWNERS: frozenset[str] = frozenset({"atlanhq"})
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_USES_RE = re.compile(r"^(?P<indent>\s*)(?:-\s*)?uses:\s*(?P<val>\S.*?)\s*$")


@dataclass(frozen=True)
class ActionUse:
    """A single `uses:` reference found in a workflow file."""

    raw_ref: str
    file: str
    line: int
    column: int


def _strip_inline_comment(value: str) -> str:
    """Strip trailing `# ...` inline comment from an unquoted value."""
    idx = value.find(" #")
    if idx != -1:
        return value[:idx].rstrip()
    return value


def _unwrap_quotes(value: str) -> str:
    """Strip surrounding single or double quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def iter_uses(text: str, file: str) -> Iterator[ActionUse]:
    """Yield every `uses:` reference in the text, skipping comment lines."""
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.lstrip()
        if stripped.startswith("#"):
            continue
        m = _USES_RE.match(raw_line)
        if not m:
            continue
        raw_val = m.group("val")
        if not (raw_val.startswith('"') or raw_val.startswith("'")):
            raw_val = _strip_inline_comment(raw_val)
        raw_val = _unwrap_quotes(raw_val)
        col = raw_line.index(m.group("val")) + 1
        yield ActionUse(raw_ref=raw_val, file=file, line=lineno, column=col)


def is_violation(raw_ref: str) -> bool:
    """Return True if this ref is a non-exempt, non-digest pin."""
    if "${{" in raw_ref:
        return False
    if raw_ref.startswith(("./", ".\\", "docker://")):
        return False
    if "@" not in raw_ref:
        return False
    owner = raw_ref.split("/")[0].lower()
    if owner in EXEMPT_OWNERS:
        return False
    ref = raw_ref.rsplit("@", 1)[1]
    if _DIGEST_RE.match(ref):
        return False
    return True


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan workflow text and return C001 findings."""
    findings: list[Finding] = []
    for use in iter_uses(text, file):
        if is_violation(use.raw_ref):
            owner_action = use.raw_ref.split("@")[0]
            ref = use.raw_ref.rsplit("@", 1)[1]
            findings.append(
                Finding(
                    rule_id=RULE_ID,
                    file=use.file,
                    line=use.line,
                    column=use.column,
                    message=(
                        f"Action '{owner_action}' is pinned to mutable ref '{ref}' — "
                        f"use a full 40-hex commit SHA. "
                        f"Exempt: atlanhq/ org (any ref), local ./ refs, "
                        f"docker:// refs, and ${{{{...}}}} templated refs."
                    ),
                    snippet=None,
                )
            )
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single workflow file, producing repo-root-relative URIs."""
    text = path.read_text(encoding="utf-8")
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def discover(root: Path) -> list[Path]:
    """Discover all workflow + composite-action files under root/.github/."""
    github_dir = root / ".github"
    if not github_dir.is_dir():
        return []
    paths: list[Path] = []
    for pattern in (
        "workflows/*.yml",
        "workflows/*.yaml",
        "actions/**/action.yml",
        "actions/**/action.yaml",
    ):
        paths.extend(github_dir.glob(pattern))
    return sorted(paths)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for C001 check."""
    parser = argparse.ArgumentParser(
        description="C001: scan GitHub Actions files for non-digest-pinned actions."
    )
    parser.add_argument(
        "scan_paths",
        nargs="*",
        default=[".github"],
        metavar="PATH",
        help="Directories or files to scan (default: .github)",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="Repo root for relative URI construction (default: .)",
    )
    parser.add_argument(
        "--sarif-output",
        metavar="FILE",
        help="Write SARIF report to FILE (default: stdout)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate emitted SARIF against the official schema",
    )
    parser.add_argument(
        "--tool-version",
        default="3.16.0",
        metavar="VERSION",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    findings: list[Finding] = []
    for raw in args.scan_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            findings.extend(scan_text(p.read_text(encoding="utf-8"), str(rel)))
        elif p.is_dir():
            all_files = sorted(p.rglob("*.yml")) + sorted(p.rglob("*.yaml"))
            for wf in all_files:
                if wf.is_file():
                    findings.extend(scan_path(wf, root))

    report = findings_to_report(findings, tool_version=args.tool_version)

    if args.validate:
        from conformance.suite.schema.validate import validate_sarif

        validate_sarif(report)

    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)
    if args.sarif_output:
        Path(args.sarif_output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main())

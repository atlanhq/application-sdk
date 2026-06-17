"""Shared CLI skeleton for single-series check modules (C, E, P, O, …).

Every series exposes the same ``main(argv)`` entry point: scan the given paths,
build a SARIF report, optionally validate it, write it out, and return the
report's exit code.  ``make_cli_main`` factors that skeleton out so each series'
``main`` collapses to one line and the arg surface can never drift between them.

The two per-series knobs — the default scan path and how a directory argument is
enumerated — are parameters, so this also serves the line-based C-series (which
defaults to ``.github`` and walks workflow YAML), not just the AST series.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol

from conformance.suite.schema.findings import Finding, findings_to_report

from ._discovery import discover as _default_discover


class CliMain(Protocol):
    """A series CLI entry point: ``main(argv=None) -> exit_code``."""

    def __call__(self, argv: list[str] | None = None) -> int: ...


try:
    TOOL_VERSION = version("atlan-application-sdk-conformance")
except PackageNotFoundError:  # pragma: no cover - source tree without install metadata
    TOOL_VERSION = "0.0.0+unknown"
"""Conformance package version, sourced once so every CLI and the runner stamp
the same ``toolVersion`` into their SARIF output."""


def make_cli_main(
    scan_text: Callable[[str, str], list[Finding]] | None = None,
    *,
    scan_all: Callable[[list[Path], Path], list[Finding]] | None = None,
    description: str,
    discover: Callable[[Path], list[Path]] = _default_discover,
    default_scan_paths: tuple[str, ...] = (".",),
    default_tool_version: str = TOOL_VERSION,
) -> CliMain:
    """Build a series ``main(argv)`` from its scan function.

    Pass exactly one of:

    * *scan_text* ``(source_text, relative_path) -> findings`` — for series
      whose rules are purely per-file (C, E, L, O, …).
    * *scan_all* ``(paths, root) -> findings`` — for series that need the full
      path list before they can run (cross-file registry rules, e.g. P-series).

    *discover* enumerates files under a directory argument; *default_scan_paths*
    is what to scan when no path is given on the CLI.  The returned callable
    parses CLI args, scans every requested path, emits SARIF, and returns the
    report exit code.
    """
    if (scan_text is None) == (scan_all is None):
        raise TypeError("exactly one of scan_text or scan_all must be provided")

    def main(argv: list[str] | None = None) -> int:
        parser = argparse.ArgumentParser(description=description)
        parser.add_argument(
            "scan_paths",
            nargs="*",
            default=list(default_scan_paths),
            metavar="PATH",
            help=f"Directories or files to scan (default: {' '.join(default_scan_paths)})",
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
            "--tool-version", default=default_tool_version, metavar="VERSION"
        )
        args = parser.parse_args(argv)

        root = Path(args.root).resolve()

        if scan_all is not None:
            collected: list[Path] = []
            for raw in args.scan_paths:
                p = Path(raw)
                if not p.is_absolute():
                    p = root / p
                if p.is_file():
                    collected.append(p)
                elif p.is_dir():
                    collected.extend(discover(p))
            findings = scan_all(collected, root)
        else:
            assert scan_text is not None
            _scan_text = scan_text  # bind non-None for closure (pyright can't narrow through closures)

            def _scan_file(path: Path) -> list[Finding]:
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = path
                return _scan_text(path.read_text(encoding="utf-8"), str(rel))

            findings = []
            for raw in args.scan_paths:
                p = Path(raw)
                if not p.is_absolute():
                    p = root / p
                if p.is_file():
                    findings.extend(_scan_file(p))
                elif p.is_dir():
                    for py_file in discover(p):
                        findings.extend(_scan_file(py_file))

        report = findings_to_report(findings, tool_version=args.tool_version)

        if args.validate:
            from conformance.suite.schema.validate import validate_sarif

            validate_sarif(report)

        payload = json.dumps(
            report.model_dump(by_alias=True, exclude_none=True), indent=2
        )
        if args.sarif_output:
            Path(args.sarif_output).write_text(payload, encoding="utf-8")
        else:
            print(payload)

        return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]

    return main

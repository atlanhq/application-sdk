"""
Validates that a connector has been fully migrated to v3.

Performs static analysis with ``re`` / ``pathlib`` — no imports, no runtime
execution. Each check is either FAIL (blocks the migration) or WARN (advisory).

Exit codes
----------
0   All FAIL checks pass (WARNs may still be present).
1   One or more FAIL checks found.
2   Usage / argument error.

Usage
-----
::

    python -m tools.migrate_v3.check_migration src/my_connector/
    python -m tools.migrate_v3.check_migration --no-color src/
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Check definitions
# ---------------------------------------------------------------------------

FAIL = "FAIL"
WARN = "WARN"
PASS = "PASS"


@dataclass
class CheckResult:
    level: str  # FAIL | WARN
    rule: str
    message: str
    file: Path
    line: int = 0
    excerpt: str = ""


# Deprecated top-level module prefixes.  Each entry matches the prefix itself
# AND any sub-module path (e.g. ``application_sdk.activities.metadata_extraction.sql``).
# The final regex appends ``(?:\.\w+)*`` to allow sub-module paths, then requires
# ``\s+import`` so it only fires on actual import statements.
_DEPRECATED_MODULE_PREFIXES = [
    # Application / entry-point
    r"application_sdk\.application",
    # Worker
    r"application_sdk\.worker",
    # Workflows
    r"application_sdk\.workflows",
    # Activities
    r"application_sdk\.activities",
    # Handlers (old)
    r"application_sdk\.handlers",
    # Services
    r"application_sdk\.services",
    # Clients — match .atlan and .atlan_auth but NOT .atlan_client (v3 path)
    r"application_sdk\.clients\.atlan(?!_client)",
    r"application_sdk\.clients\.temporal",
    r"application_sdk\.clients\.workflow",
    # Interceptors
    r"application_sdk\.interceptors",
    # Test utilities (old)
    r"application_sdk\.test_utils",
]

_RE_DEPRECATED_IMPORT = re.compile(
    r"(?:^|(?<=\n))[ \t]*from\s+(?:"
    + "|".join(f"(?:{p})" for p in _DEPRECATED_MODULE_PREFIXES)
    + r")(?:\.\w+)*\s+import\b",
    re.MULTILINE,
)

# v2 Temporal decorators that must not appear in migrated code.
_RE_V2_DECORATORS = re.compile(r"@(?:workflow\.defn|activity\.defn|auto_heartbeater)\b")

# Direct workflow.execute_activity_method calls.
_RE_EXECUTE_ACTIVITY = re.compile(r"\bworkflow\.execute_activity_method\s*\(")

# Sync get_client() call (async get_async_client is fine).
# Negative lookbehind for "async" and "get_async_" to avoid false positives.
_RE_SYNC_GET_CLIENT = re.compile(r"(?<!\bget_async_)(?<!\basync\s)\bget_client\s*\(")

# App subclass existence — class inheriting from a v3 base.
_RE_APP_SUBCLASS = re.compile(
    r"class\s+\w+\s*\(\s*(?:"
    r"App|SqlMetadataExtractor|IncrementalSqlMetadataExtractor|"
    r"SqlQueryExtractor|BaseMetadataExtractor"
    r")\s*[,)]"
)

# Dict[str, Any] or dict[str, Any] near a @task decorator (warn only).
_RE_DICT_ANY = re.compile(
    r"\bDict\s*\[\s*str\s*,\s*Any\s*\]|\bdict\s*\[\s*str\s*,\s*Any\s*\]"
)

# Handler using new base.
_RE_HANDLER_BASE = re.compile(r"class\s+\w+\s*\(\s*Handler\s*[,)]")

# Handler methods that still use *args / **kwargs instead of typed contracts.
_RE_HANDLER_KWARGS = re.compile(
    r"async\s+def\s+(?:test_auth|preflight_check|fetch_metadata)"
    r"\s*\([^)]*(?:\*args|\*\*kwargs)[^)]*\)"
)

# allow_unbounded_fields=True — escape hatch forbidden in connector contracts.
_RE_UNBOUNDED_ESCAPE = re.compile(r"allow_unbounded_fields\s*=\s*True")

# Entry point: run_dev_combined or application-sdk CLI reference.
_RE_ENTRY_POINT = re.compile(r"run_dev_combined\s*\(|application[-_]sdk\s+--mode\b")


# ---------------------------------------------------------------------------
# Core checker
# ---------------------------------------------------------------------------


def _iter_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _find_pattern(
    lines: list[str],
    pattern: re.Pattern[str],
    *,
    path: Path,
    level: str,
    rule: str,
    message_template: str,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for lineno, line in enumerate(lines, start=1):
        if pattern.search(line):
            results.append(
                CheckResult(
                    level=level,
                    rule=rule,
                    message=message_template,
                    file=path,
                    line=lineno,
                    excerpt=line.strip(),
                )
            )
    return results


def check_file(path: Path) -> list[CheckResult]:
    """Run all checks on a single Python file. Returns a list of findings."""
    lines = _iter_lines(path)
    if not lines:
        return []

    results: list[CheckResult] = []

    # ── FAIL: deprecated imports ──────────────────────────────────────────
    results += _find_pattern(
        lines,
        _RE_DEPRECATED_IMPORT,
        path=path,
        level=FAIL,
        rule="no-deprecated-imports",
        message_template="Deprecated v2 import — run the import rewriter first.",
    )

    # ── FAIL: v2 decorators ───────────────────────────────────────────────
    results += _find_pattern(
        lines,
        _RE_V2_DECORATORS,
        path=path,
        level=FAIL,
        rule="no-v2-decorators",
        message_template=(
            "@workflow.defn / @activity.defn / @auto_heartbeater removed. "
            "Use @task and merge into an App subclass."
        ),
    )

    # ── FAIL: execute_activity_method ────────────────────────────────────
    results += _find_pattern(
        lines,
        _RE_EXECUTE_ACTIVITY,
        path=path,
        level=FAIL,
        rule="no-execute-activity-method",
        message_template=(
            "workflow.execute_activity_method() removed. "
            "Define @task methods directly on the App subclass."
        ),
    )

    # ── FAIL: sync get_client ─────────────────────────────────────────────
    results += _find_pattern(
        lines,
        _RE_SYNC_GET_CLIENT,
        path=path,
        level=FAIL,
        rule="no-sync-get-client",
        message_template=(
            "Sync get_client() removed. "
            "Use async create_async_atlan_client() with AtlanApiToken credential."
        ),
    )

    full_text = "\n".join(lines)

    # ── FAIL: handler methods with *args / **kwargs ───────────────────────
    # Only fires when this file defines a Handler subclass.
    if _RE_HANDLER_BASE.search(full_text):
        results += _find_pattern(
            lines,
            _RE_HANDLER_KWARGS,
            path=path,
            level=FAIL,
            rule="handler-typed-signatures",
            message_template=(
                "Handler method uses *args / **kwargs — replace with typed contracts: "
                "test_auth(self, input: AuthInput), "
                "preflight_check(self, input: PreflightInput), "
                "fetch_metadata(self, input: MetadataInput). "
                "See §4 of MIGRATION_PROMPT.md."
            ),
        )

    # ── FAIL: allow_unbounded_fields=True in connector code ──────────────
    results += _find_pattern(
        lines,
        _RE_UNBOUNDED_ESCAPE,
        path=path,
        level=FAIL,
        rule="no-unbounded-escape-hatch",
        message_template=(
            "allow_unbounded_fields=True is forbidden in connector contracts. "
            "Use Annotated[list[T], MaxItems(N)] or FileReference for large data. "
            "See §7 of MIGRATION_PROMPT.md."
        ),
    )

    # ── WARN: Dict[str, Any] near @task ──────────────────────────────────
    # Only warn if the file also contains @task (avoids noise in unrelated files).
    if re.search(r"@task\b", full_text):
        results += _find_pattern(
            lines,
            _RE_DICT_ANY,
            path=path,
            level=WARN,
            rule="typed-task-signatures",
            message_template=(
                "Dict[str, Any] found near @task — use typed Input/Output dataclasses. "
                "See migration guide Step 7."
            ),
        )

    return results


def check_directory(
    root: Path,
    *,
    app_subclass_required: bool = True,
    entry_point_required: bool = True,
) -> tuple[list[CheckResult], list[str]]:
    """
    Check all Python files under *root*.

    Returns (per-file findings, directory-level advisory messages).
    """
    py_files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
    all_results: list[CheckResult] = []
    combined_text = ""

    for path in py_files:
        all_results += check_file(path)
        try:
            combined_text += path.read_text(encoding="utf-8") + "\n"
        except OSError:
            pass

    advisories: list[str] = []

    # ── WARN: no App subclass found ───────────────────────────────────────
    if app_subclass_required and not _RE_APP_SUBCLASS.search(combined_text):
        advisories.append(
            "WARN [app-subclass-missing]: No App subclass found. "
            "Create a class that inherits from App / SqlMetadataExtractor / "
            "IncrementalSqlMetadataExtractor / SqlQueryExtractor."
        )

    # ── WARN: handler not using new base ─────────────────────────────────
    if re.search(
        r"class\s+\w+\s*\(.*Handler", combined_text
    ) and not _RE_HANDLER_BASE.search(combined_text):
        advisories.append(
            "WARN [handler-base]: Handler class found but does not inherit from the v3 "
            "Handler base (application_sdk.handler.Handler)."
        )

    # ── WARN: entry point not updated ────────────────────────────────────
    if entry_point_required and not _RE_ENTRY_POINT.search(combined_text):
        advisories.append(
            "WARN [entry-point]: No run_dev_combined() call or CLI reference found. "
            "Update the entry point to use 'application-sdk --mode combined' or "
            "asyncio.run(run_dev_combined(MyApp, handler_class=MyHandler))."
        )

    # ── WARN: v2 directory structure still present ────────────────────────
    if root.is_dir():
        for subdir in sorted(root.rglob("*")):
            if subdir.is_dir() and subdir.name in ("activities", "workflows"):
                advisories.append(
                    f"WARN [no-v2-directory-structure]: v2 directory '{subdir}' still "
                    "present. Consolidate into app/<app_name>.py and delete the empty "
                    "directory. See Phase 2c of the migration skill."
                )

    return all_results, advisories


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_ANSI = {
    FAIL: "\033[91m",  # red
    WARN: "\033[93m",  # yellow
    PASS: "\033[92m",  # green
    "reset": "\033[0m",
}


def _color(text: str, level: str, *, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_ANSI.get(level, '')}{text}{_ANSI['reset']}"


def print_report(
    results: list[CheckResult],
    advisories: list[str],
    *,
    use_color: bool = True,
) -> bool:
    """
    Print a structured report.

    Returns True if all FAIL checks pass (exit-code 0 semantics).
    """
    fail_count = sum(1 for r in results if r.level == FAIL)
    warn_count = sum(1 for r in results if r.level == WARN) + len(advisories)

    if not results and not advisories:
        print(_color("✓ All migration checks passed.", PASS, use_color=use_color))
        return True

    # Group by file for readability.
    by_file: dict[Path, list[CheckResult]] = {}
    for r in results:
        by_file.setdefault(r.file, []).append(r)

    for path, file_results in sorted(by_file.items()):
        print(f"\n{path}")
        for r in file_results:
            tag = _color(f"[{r.level}]", r.level, use_color=use_color)
            loc = f"line {r.line}: " if r.line else ""
            print(f"  {tag} [{r.rule}] {loc}{r.message}")
            if r.excerpt:
                print(f"        {r.excerpt}")

    if advisories:
        print()
        for advisory in advisories:
            level = WARN if advisory.startswith("WARN") else FAIL
            print(_color(advisory, level, use_color=use_color))

    print()
    status = (
        _color("PASS", PASS, use_color=use_color)
        if fail_count == 0
        else _color("FAIL", FAIL, use_color=use_color)
    )
    print(
        f"Result: {status}  "
        f"({_color(str(fail_count) + ' failure(s)', FAIL, use_color=use_color)}, "
        f"{_color(str(warn_count) + ' warning(s)', WARN, use_color=use_color)})"
    )
    return fail_count == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether a connector has been fully migrated to v3. "
            "Exit 0 = all FAILs pass; exit 1 = failures remain."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets",
        nargs="+",
        metavar="PATH",
        help="Python file(s) or directory to check.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour output.",
    )
    parser.add_argument(
        "--no-app-check",
        action="store_true",
        help="Skip the 'App subclass exists' directory-level check.",
    )
    parser.add_argument(
        "--no-entry-point-check",
        action="store_true",
        help="Skip the 'entry point updated' directory-level check.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    use_color = not args.no_color and sys.stdout.isatty()

    all_results: list[CheckResult] = []
    all_advisories: list[str] = []

    for raw in args.targets:
        target = Path(raw)
        if not target.exists():
            print(f"ERROR: path does not exist: {target}", file=sys.stderr)
            return 2

        results, advisories = check_directory(
            target,
            app_subclass_required=not args.no_app_check,
            entry_point_required=not args.no_entry_point_check,
        )
        all_results.extend(results)
        all_advisories.extend(advisories)

    passed = print_report(all_results, all_advisories, use_color=use_color)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

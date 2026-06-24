"""C004 ForceExternalRuntimeDrift — flag connector workflows that still force
the external Dapr/Temporal runtime in CI.

The shared ``connector-integration-tests`` GitHub Action accepts a
``force-external-runtime`` input. When ``"true"`` it installs + starts an
external ``daprd`` (and a Temporal dev server) before launching the app.

That flag is only needed on ``atlan-application-sdk < 3.13``. On ``>= 3.13`` the
standard connector entry point ``run_dev_combined()`` already boots an
in-process (embedded) ``daprd`` + Temporal itself, so forcing the external
runtime is redundant — it stands up a *second* runtime in CI (the embedded one
roots its object store at ``./local/objectstore`` while the external one roots
at ``./local/dapr/objectstore``). That is wasteful and a known source of
"component not found" / wrong-objectstore-root flakiness.

This is drift: connectors needed the flag pre-3.13, bumped their SDK pin, and
never removed it. The v3 reference connectors (atlan-metabase-app,
atlan-openapi-app, atlan-mysql-app) do *not* set it — they run embedded.

The rule is WARN (never blocks CI) and presence-based: it flags every
``force-external-runtime: true`` it finds in a workflow file. A connector that
is genuinely still on SDK < 3.13 is the legitimate exception and can acknowledge
it inline with ``# conformance: ignore[C004] <reason>``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import make_cli_main
from conformance.suite.schema.findings import Finding

SERIES = "C"
RULE_ID = "C004"

# A workflow line that sets the flag truthy: ``force-external-runtime: true`` /
# ``: "true"`` / ``: 'true'``, with optional surrounding quotes and an optional
# trailing ``# comment``. ``false`` (the default) is intentionally not matched.
_FLAG_RE = re.compile(
    r"""^(?P<indent>\s*)force-external-runtime\s*:\s*["']?true["']?\s*(?:\#.*)?$""",
)

# Inline suppression directive, identical form to the D-/E-series so users only
# learn one syntax: ``# conformance: ignore[C004] reason``. A directive on the
# violating line or the line directly above suppresses the finding.
_SUPPRESS_RE = re.compile(
    r"^#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)

_MESSAGE = (
    "Workflow sets `force-external-runtime: true`, which makes the "
    "connector-integration-tests action stand up an external daprd + Temporal. "
    "On atlan-application-sdk >= 3.13 the standard `run_dev_combined()` entry "
    "(main.py) already boots an in-process (embedded) daprd + Temporal, so this "
    "flag is redundant and starts a SECOND runtime in CI — wasteful and a known "
    "source of objectstore-root / 'component not found' flakiness. Once your "
    "integration suite passes without it, drop this line to run the embedded "
    "runtime (as the atlan-metabase-app / atlan-openapi-app / atlan-mysql-app "
    "reference connectors do). If you are genuinely still on SDK < 3.13, this is "
    "expected — acknowledge it with `# conformance: ignore[C004] <reason>`."
)


def _parse_suppressions(text: str) -> dict[int, tuple[frozenset[str] | None, str]]:
    """Return ``{lineno: (rule_ids_or_None, justification)}`` for every
    ``# conformance: ignore[...]`` directive in *text*.

    ``rule_ids_or_None`` is ``None`` for a directive with no ``[...]`` list
    (matches any rule). The directive may stand on its own comment line or trail
    a value as ``force-external-runtime: true  # conformance: ignore[C004] ...``.
    """
    out: dict[int, tuple[frozenset[str] | None, str]] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        idx = raw.find("#")
        if idx == -1:
            continue
        m = _SUPPRESS_RE.match(raw[idx:].strip())
        if m is None:
            continue
        ids_blob = (m.group(1) or "").strip()
        ids: frozenset[str] | None = (
            None
            if not ids_blob
            else frozenset(s.strip() for s in ids_blob.split(",") if s.strip())
        )
        out[lineno] = (ids, m.group(2).strip())
    return out


def _is_suppressed(
    suppressions: dict[int, tuple[frozenset[str] | None, str]],
    line: int,
) -> tuple[bool, str | None]:
    """Return ``(suppressed, justification)`` for a finding at *line*.

    A directive on the same line or the line immediately above suppresses the
    finding when its rule-id list is empty or contains ``C004``.
    """
    for cand in (line, line - 1):
        if cand not in suppressions:
            continue
        ids, just = suppressions[cand]
        if ids is None or RULE_ID in ids:
            return True, just
    return False, None


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan workflow text and return C004 findings."""
    suppressions = _parse_suppressions(text)
    findings: list[Finding] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.lstrip()
        if stripped.startswith("#"):
            continue  # a commented-out flag is not active
        m = _FLAG_RE.match(raw_line)
        if m is None:
            continue
        suppressed, justification = _is_suppressed(suppressions, lineno)
        findings.append(
            Finding(
                rule_id=RULE_ID,
                file=file,
                line=lineno,
                column=len(m.group("indent")) + 1,
                message=_MESSAGE,
                snippet=None,
                suppressed=suppressed,
                suppression_justification=justification,
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
    """Discover workflow files under ``root/.github/workflows/``.

    The flag lives in caller *workflows* (``tests.yaml`` et al.), not composite
    actions, so — unlike C001 — this does not scan ``.github/actions/``.
    """
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    paths: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        paths.extend(workflows.glob(pattern))
    return sorted(paths)


def _walk_workflow_files(path: Path) -> list[Path]:
    """Enumerate YAML files under a directory argument (CLI dir-scan)."""
    return sorted(path.rglob("*.yml")) + sorted(path.rglob("*.yaml"))


main = make_cli_main(
    scan_text,
    description=(
        "C004: flag connector workflows that force the external Dapr/Temporal "
        "runtime (redundant on SDK >= 3.13)."
    ),
    discover=_walk_workflow_files,
    default_scan_paths=(".github",),
)
"""CLI entry point for the C004 check."""


if __name__ == "__main__":
    sys.exit(main())

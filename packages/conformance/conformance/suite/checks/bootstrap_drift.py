"""C002 BootstrapWorkflowDrift — detect managed CI workflows that are absent or drifted.

The ``atlan-application-sdk-conformance bootstrap`` command installs a standard
set of CI workflow shims.  This check flags any managed file that is:

- missing (never bootstrapped, or accidentally deleted), or
- structurally drifted from what ``bootstrap`` would write.

For the two parameterised templates (``build-and-publish.yaml`` and
``docstring-coverage.yaml``) the per-repo custom value is *extracted from the
on-disk file* before comparing, so intentional per-repo choices are not flagged
as drift — only structural changes are caught.

Remediation: run ``atlan-application-sdk-conformance bootstrap`` to re-sync.
"""

from __future__ import annotations

import re
from pathlib import Path

from conformance.bootstrap.render import MANAGED_WORKFLOWS, render
from conformance.suite.schema.findings import Finding

SERIES = "C"
RULE_ID = "C002"

_CLI_CMD = "atlan-application-sdk-conformance bootstrap"

# Regexes to extract the per-repo customised values from the two templated
# files so drift comparisons are structural, not literal.
_PKG_NAME_RE = re.compile(r'package_name:\s+"([^"]+)"')
_UNIT_TESTS_WF_RE = re.compile(r'unit_tests_workflow_file:\s+"([^"]+)"')


def _extract_package_name(text: str) -> str:
    m = _PKG_NAME_RE.search(text)
    return m.group(1) if m else "app"


def _extract_unit_tests_workflow(text: str) -> str:
    m = _UNIT_TESTS_WF_RE.search(text)
    return m.group(1) if m else "tests.yaml"


def discover(root: Path) -> list[Path]:
    """Return expected managed workflow paths under root/.github/workflows/.

    Paths are returned whether or not they exist; ``scan_path`` handles the
    missing-file case so absent shims are reported as findings.
    """
    wf_dir = root / ".github" / "workflows"
    return [wf_dir / name for name in MANAGED_WORKFLOWS]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Return C002 findings for *path* (may or may not exist on disk)."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    name = path.name

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Managed CI workflow '{name}' is absent. "
                    f"Run `{_CLI_CMD}` to install the standard shim."
                ),
            )
        ]

    on_disk = path.read_text(encoding="utf-8")

    # For parameterised templates, extract the on-disk value so structural
    # drift is caught while per-repo value choices are preserved.
    kwargs: dict[str, str] = {}
    if name == "docstring-coverage.yaml":
        kwargs["package_name"] = _extract_package_name(on_disk)
    elif name == "build-and-publish.yaml":
        kwargs["unit_tests_workflow"] = _extract_unit_tests_workflow(on_disk)

    canonical = render(name, **kwargs)

    if on_disk == canonical:
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            file=rel,
            line=1,
            column=1,
            message=(
                f"CI workflow '{name}' has drifted from the bootstrap canonical. "
                f"Run `{_CLI_CMD}` to re-sync."
            ),
        )
    ]

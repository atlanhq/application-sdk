"""C002 BootstrapWorkflowDrift — detect managed CI workflows that are absent or drifted.

The ``atlan-application-sdk-conformance bootstrap`` command installs a standard
set of CI workflow shims.  This check flags any managed file that is:

- missing (never bootstrapped, or accidentally deleted), or
- structurally drifted from what ``bootstrap`` would write.

For parameterised templates the per-repo custom values are *extracted from the
on-disk file* before comparing, so intentional per-repo choices (app-name,
package_name, etc.) are not flagged as drift — only structural changes are caught.

**Two drift tracks:**

1. **Managed shims** (``MANAGED_WORKFLOWS`` — 14 files): always-overwrite.
   Absent or drifted → WARN finding; run bootstrap to re-sync (re-runs overwrite).

2. **tests.yaml**: write-if-absent scaffold.  Bootstrap creates it once and
   never clobbers customisations.  Drift is also tracked at WARN, never BLOCK.
   Remediation: *delete tests.yaml* then re-run bootstrap to regenerate from
   canonical.

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

# Write-if-absent scaffolds tracked alongside managed shims (WARN-only drift).
_TESTS_WORKFLOW = "tests.yaml"
_RENOVATE_JSON = "renovate.json"

# ---------------------------------------------------------------------------
# Managed-shim param extractors
# ---------------------------------------------------------------------------

# Regexes to extract the per-repo customised values from the two templated
# managed-shim files so drift comparisons are structural, not literal.
_PKG_NAME_RE = re.compile(r'package_name:\s+"([^"]+)"')
_UNIT_TESTS_WF_RE = re.compile(r'unit_tests_workflow_file:\s+"([^"]+)"')


def _extract_package_name(text: str) -> str:
    m = _PKG_NAME_RE.search(text)
    return m.group(1) if m else "app"


def _extract_unit_tests_workflow(text: str) -> str:
    m = _UNIT_TESTS_WF_RE.search(text)
    return m.group(1) if m else "tests.yaml"


# ---------------------------------------------------------------------------
# tests.yaml param extractors
# ---------------------------------------------------------------------------

_APP_NAME_RE = re.compile(r'app-name:\s+"([^"]+)"')
_APP_IMAGE_NAME_RE = re.compile(r'app-image-name:\s+"([^"]+)"')
_ENABLE_E2E_RE = re.compile(r"enable-e2e:\s+(true|false)")
# Matches an *uncommented* services-script line (quoted value) in the with: block.
_SERVICES_SCRIPT_RE = re.compile(r'^\s+services-script:\s+"([^"]+)"$', re.MULTILINE)


def _extract_tests_yaml_params(text: str) -> dict[str, str]:
    """Extract the per-repo customised values from a scaffolded tests.yaml.

    Returns only the keys that were found; callers should pass these as kwargs
    to ``render("tests.yaml", ...)`` so defaults apply for any that are absent.
    """
    params: dict[str, str] = {}
    m = _APP_NAME_RE.search(text)
    if m:
        params["app_name"] = m.group(1)
    m = _APP_IMAGE_NAME_RE.search(text)
    if m:
        params["app_image_name"] = m.group(1)
    m = _ENABLE_E2E_RE.search(text)
    if m:
        params["enable_e2e"] = m.group(1)
    m = _SERVICES_SCRIPT_RE.search(text)
    if m:
        params["services_script"] = m.group(1).strip()
    return params


# ---------------------------------------------------------------------------
# Discovery + scanning
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Return expected managed + scaffold paths for this repo.

    Paths are returned whether or not they exist; ``scan_path`` handles the
    missing-file case so absent shims are reported as findings.
    """
    wf_dir = root / ".github" / "workflows"
    paths = [wf_dir / name for name in MANAGED_WORKFLOWS]
    # Write-if-absent scaffolds (WARN-only drift tracking).
    paths.append(wf_dir / _TESTS_WORKFLOW)
    paths.append(root / _RENOVATE_JSON)
    return paths


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Return C002 findings for *path* (may or may not exist on disk)."""
    if path.name == _TESTS_WORKFLOW:
        return _scan_tests_yaml(path, root)
    if path.name == _RENOVATE_JSON:
        return _scan_renovate_json(path, root)
    return _scan_managed_shim(path, root)


def _scan_managed_shim(path: Path, root: Path) -> list[Finding]:
    """Scan one of the always-managed workflow shims."""
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


def _scan_renovate_json(path: Path, root: Path) -> list[Finding]:
    """Scan the write-if-absent renovate.json scaffold — WARN-only, never BLOCK."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    _remediate = (
        f"To regenerate, delete renovate.json and re-run `{_CLI_CMD}` "
        f"(drift is informational — WARN only, never blocks CI)."
    )

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Scaffolded renovate.json is absent. "
                    f"Run `{_CLI_CMD}` to regenerate it. " + _remediate
                ),
            )
        ]

    on_disk = path.read_text(encoding="utf-8")
    canonical = render(_RENOVATE_JSON)

    if on_disk == canonical:
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            file=rel,
            line=1,
            column=1,
            message=(
                "Scaffolded renovate.json has drifted from the bootstrap canonical. "
                + _remediate
            ),
        )
    ]


def _scan_tests_yaml(path: Path, root: Path) -> list[Finding]:
    """Scan the write-if-absent tests.yaml scaffold — WARN-only, never BLOCK."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    _remediate = (
        f"To regenerate, delete tests.yaml and re-run `{_CLI_CMD}` "
        f"(drift is informational — WARN only, never blocks CI)."
    )

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Scaffolded tests.yaml is absent. "
                    f"Run `{_CLI_CMD}` to regenerate it. " + _remediate
                ),
            )
        ]

    on_disk = path.read_text(encoding="utf-8")

    # Extract per-repo customised values so structural drift is caught while
    # legitimate param choices (app-name, enable-e2e, services-script) are not.
    params = _extract_tests_yaml_params(on_disk)
    canonical = render(_TESTS_WORKFLOW, **params)

    if on_disk == canonical:
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            file=rel,
            line=1,
            column=1,
            message=(
                "Scaffolded tests.yaml has drifted from the bootstrap canonical "
                "(structural changes detected; param customizations are not flagged). "
                + _remediate
            ),
        )
    ]

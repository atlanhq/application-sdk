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

**Sanctioned per-repo overrides:** a few managed shims accept a documented
override that is normalised out of the on-disk file before comparison, so the
choice is not flagged as drift (any *other* change still is).  Currently:
``renovate-pkl-sync.yaml`` tolerates a ``with: regenerate-contract: <bool>``
opt-out on its reusable-workflow caller, and ``tests.yaml`` tolerates an
``e2e-parallel-workers: <count|auto>`` input opting the e2e job into
pytest-xdist parallelism (default is sequential).

Remediation: run ``atlan-application-sdk-conformance bootstrap`` to re-sync.
"""

from __future__ import annotations

import re
from pathlib import Path

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_field,
    extract_renovate_automerge,
    resolve_renovate_fallback_exit_zero,
)
from conformance.bootstrap.render import MANAGED_ACTION_FILES, MANAGED_WORKFLOWS, render
from conformance.suite.schema.findings import Finding

# Dest-path (repo-root-relative) -> template filename, for O(1) lookup in scan_path.
_MANAGED_ACTION_FILES_BY_DEST = dict(MANAGED_ACTION_FILES)

SERIES = "C"
RULE_ID = "C002"

_CLI_CMD = "atlan-application-sdk-conformance bootstrap"

# Write-if-absent scaffolds tracked alongside managed shims (WARN-only drift).
_TESTS_WORKFLOW = "tests.yaml"
_RENOVATE_JSON = "renovate.json"

# Managed shim (in MANAGED_WORKFLOWS) that tolerates a sanctioned per-repo override.
_RENOVATE_PKL_SYNC = "renovate-pkl-sync.yaml"

# Matches a pinned SHA (40 lowercase hex chars) and its optional trailing version
# comment so automated pin bumps (Renovate/Dependabot) are not flagged as drift.
# Example: "@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3" → "@<pinned>"
_ACTION_PIN_RE = re.compile(r"@[0-9a-f]{40}(?:[ \t]+#[^\n]*)?")


def _strip_action_pins(text: str) -> str:
    return _ACTION_PIN_RE.sub("@<pinned>", text)


# The renovate-pkl-sync caller regenerates contract artifacts by default
# (the reusable's regenerate-contract input defaults to true). An app may opt
# out by adding a `with: regenerate-contract: false` override to the caller.
# That override is a sanctioned per-repo choice, so strip it before the drift
# comparison — only other structural changes should flag C002. Matches the
# block whether the value is true or false and regardless of indentation.
_REGEN_OVERRIDE_RE = re.compile(
    r"\n[ ]*with:\n[ ]*regenerate-contract:[ ]*(?:true|false)[ ]*"
)


def _strip_regen_override(text: str) -> str:
    return _REGEN_OVERRIDE_RE.sub("", text)


# tests.yaml runs the e2e suite sequentially by default; a connector whose e2e
# test classes are mutually independent may opt into pytest-xdist parallelism by
# adding an `e2e-parallel-workers` input to its tests-reusable caller. That is a
# sanctioned per-repo choice, so strip it (and any single comment line directly
# above it) before the drift comparison — only other structural changes should
# flag C002. The accepted values mirror the runtime driver
# (build_pytest_parallel_args.py): "auto" or a positive integer with no leading
# zero, quoted or bare. An invalid value like "0"/"01" is deliberately NOT
# stripped, so C002 flags it as drift before the driver rejects it at runtime.
_E2E_PARALLEL_OVERRIDE_RE = re.compile(
    r'\n(?:[ ]*#[^\n]*\n)?[ ]*e2e-parallel-workers:[ ]*"?(?:auto|[1-9][0-9]*)"?[ ]*'
)


def _strip_e2e_parallel_workers(text: str) -> str:
    return _E2E_PARALLEL_OVERRIDE_RE.sub("", text)


# ---------------------------------------------------------------------------
# Managed-shim param extractors
#
# The shared "read a rendered param back off an on-disk managed file"
# primitives (``extract_field``, ``extract_renovate_automerge``,
# ``EXIT_ZERO_RE``) live in ``conformance.bootstrap.extract`` — a leaf module
# with no dependency on this one — so both this checker's drift-comparison
# extractors and ``bootstrap``'s own re-run autodetection can import them at
# module level without an import cycle.
# ---------------------------------------------------------------------------


def _extract_package_name(text: str) -> str:
    return extract_field(text, "package_name") or "app"


def _extract_unit_tests_workflow(text: str) -> str:
    return extract_field(text, "unit_tests_workflow_file") or "tests.yaml"


def _extract_exit_zero(text: str, root: Path) -> str:
    """Return the on-disk ``exit-zero`` value, falling back to *root*'s
    ``renovate.json`` enforcement signal when the line is unparseable.

    Uses ``resolve_renovate_fallback_exit_zero`` — the same automerge-to-
    exit-zero decision ``bootstrap.autodetect._read_conformance_enforce``
    falls back to — so this checker and ``bootstrap``'s own re-run
    autodetection cannot silently diverge on a hand-edited/pre-template
    ``conformance.yaml``: without this, a genuinely soft-mode repo whose
    exit-zero line doesn't match the pattern would report a spurious C002
    drift finding here while `bootstrap` itself correctly preserves soft
    mode via the same renovate.json fallback.
    """
    m = EXIT_ZERO_RE.search(text)
    if m:
        return m.group(1)
    renovate = root / _RENOVATE_JSON
    if not renovate.exists():
        return "false"
    try:
        renovate_text = renovate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "false"
    return resolve_renovate_fallback_exit_zero(renovate_text)


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
    # Non-workflow vendored files (composite action + arg-building script).
    paths.extend(root / dest_rel for dest_rel in _MANAGED_ACTION_FILES_BY_DEST)
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
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = None
    if rel in _MANAGED_ACTION_FILES_BY_DEST:
        return _scan_managed_action_file(path, root, _MANAGED_ACTION_FILES_BY_DEST[rel])
    return _scan_managed_shim(path, root)


def _scan_managed_action_file(
    path: Path, root: Path, template_name: str
) -> list[Finding]:
    """Scan one of the vendored non-workflow files (action.yaml / scripts)."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Managed file '{rel}' is absent. Run `{_CLI_CMD}` to install it."
                ),
            )
        ]

    on_disk = path.read_text(encoding="utf-8")
    canonical = render(template_name)

    if _strip_action_pins(on_disk) == _strip_action_pins(canonical):
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            file=rel,
            line=1,
            column=1,
            message=(
                f"Managed file '{rel}' has drifted from the bootstrap canonical. "
                f"Run `{_CLI_CMD}` to re-sync."
            ),
        )
    ]


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

    # Sanctioned per-repo override: opting out of contract regeneration is not
    # drift. Strip it before comparing to the (plain) canonical caller.
    if name == _RENOVATE_PKL_SYNC:
        on_disk = _strip_regen_override(on_disk)

    # For parameterised templates, extract the on-disk value so structural
    # drift is caught while per-repo value choices are preserved.
    kwargs: dict[str, str] = {}
    if name == "docstring-coverage.yaml":
        kwargs["package_name"] = _extract_package_name(on_disk)
    elif name == "build-and-publish.yaml":
        kwargs["unit_tests_workflow"] = _extract_unit_tests_workflow(on_disk)
    elif name == "conformance.yaml":
        kwargs["exit_zero"] = _extract_exit_zero(on_disk, root)

    canonical = render(name, **kwargs)

    if _strip_action_pins(on_disk) == _strip_action_pins(canonical):
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
    canonical = render(_RENOVATE_JSON, automerge=extract_renovate_automerge(on_disk))

    if _strip_action_pins(on_disk) == _strip_action_pins(canonical):
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

    # Opting into parallel e2e (e2e-parallel-workers) is a sanctioned per-repo
    # choice — normalise it out so only other structural changes flag drift.
    normalized = _strip_e2e_parallel_workers(on_disk)
    if _strip_action_pins(normalized) == _strip_action_pins(canonical):
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

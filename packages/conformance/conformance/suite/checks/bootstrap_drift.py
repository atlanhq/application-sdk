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

2. **tests.yaml / renovate.json**: write-if-absent scaffolds.  Bootstrap
   creates each once and never clobbers customisations.  Drift is also tracked
   at WARN, never BLOCK.  Remediation: re-run bootstrap with ``--resync``,
   which re-renders them from the canonical while reusing the same values
   extracted here — so the structural catch-up lands and the per-repo choices
   survive.

Remediation: run ``atlan-application-sdk-conformance bootstrap`` to re-sync.
"""

from __future__ import annotations

from pathlib import Path

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_apt_packages,
    extract_field,
    extract_renovate_automerge,
    extract_tests_yaml_params,
    resolve_renovate_fallback_exit_zero,
    strip_action_pins,
)
from conformance.bootstrap.render import MANAGED_ACTION_FILES, MANAGED_WORKFLOWS, render
from conformance.suite.checks._ast_common import safe_read_text
from conformance.suite.schema.findings import Finding

# Dest-path (repo-root-relative) -> template filename, for O(1) lookup in scan_path.
_MANAGED_ACTION_FILES_BY_DEST = dict(MANAGED_ACTION_FILES)

SERIES = "C"
RULE_ID = "C002"

_CLI_CMD = "atlan-application-sdk-conformance bootstrap"

# Write-if-absent scaffolds tracked alongside managed shims (WARN-only drift).
_TESTS_WORKFLOW = "tests.yaml"
_RENOVATE_JSON = "renovate.json"

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

    on_disk = safe_read_text(path)
    if on_disk is None:
        return []
    canonical = render(template_name)

    if strip_action_pins(on_disk) == strip_action_pins(canonical):
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

    on_disk = safe_read_text(path)
    if on_disk is None:
        return []

    # For parameterised templates, extract the on-disk value so structural
    # drift is caught while per-repo value choices are preserved.
    kwargs: dict[str, str] = {}
    if name == "docstring-coverage.yaml":
        kwargs["package_name"] = _extract_package_name(on_disk)
    elif name == "build-and-publish.yaml":
        kwargs["unit_tests_workflow"] = _extract_unit_tests_workflow(on_disk)
    elif name == "conformance.yaml":
        kwargs["exit_zero"] = _extract_exit_zero(on_disk, root)
    elif name == "checks.yml":
        # The optional system-deps step is a per-repo value like any other
        # rendered param: a repo that legitimately needs build headers before
        # `uv sync` keeps them, and only structural drift is flagged. Without
        # this, every such repo would report permanent C002 drift whose only
        # "fix" (re-run bootstrap) deletes the step its CI needs.
        kwargs["system_deps"] = extract_apt_packages(on_disk)

    canonical = render(name, **kwargs)

    if strip_action_pins(on_disk) == strip_action_pins(canonical):
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

    _warn_only = "Drift is informational — WARN only, never blocks CI."

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Scaffolded renovate.json is absent. Run `{_CLI_CMD}` to "
                    f"scaffold it (write-if-absent: a bare re-run is enough, "
                    f"nothing to preserve). " + _warn_only
                ),
            )
        ]

    on_disk = safe_read_text(path)
    if on_disk is None:
        return []
    canonical = render(_RENOVATE_JSON, automerge=extract_renovate_automerge(on_disk))

    if strip_action_pins(on_disk) == strip_action_pins(canonical):
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            file=rel,
            line=1,
            column=1,
            message=(
                "Scaffolded renovate.json has drifted from the bootstrap canonical. "
                f"Run `{_CLI_CMD} --resync` to re-render it, keeping the auto-merge "
                "mode this file already declares — pass --enforce or "
                "--renovate-automerge instead only to deliberately CHANGE that mode. "
                "Any other hand edit is replaced (kept as renovate.json.bak). "
                "If --resync reports it skipped (the file isn't valid JSON, so "
                "its mode can't be read back), it needs a manual fix. " + _warn_only
            ),
        )
    ]


def _scan_tests_yaml(path: Path, root: Path) -> list[Finding]:
    """Scan the write-if-absent tests.yaml scaffold — WARN-only, never BLOCK."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    _warn_only = "Drift is informational — WARN only, never blocks CI."

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Scaffolded tests.yaml is absent. Run `{_CLI_CMD}` to "
                    f"scaffold it (write-if-absent: a bare re-run is enough, "
                    f"nothing to preserve). " + _warn_only
                ),
            )
        ]

    on_disk = safe_read_text(path)
    if on_disk is None:
        return []

    # Extract per-repo customised values so structural drift is caught while
    # legitimate param choices (app-name, enable-e2e, services-script) are not.
    params = extract_tests_yaml_params(on_disk)
    canonical = render(_TESTS_WORKFLOW, **params)

    if strip_action_pins(on_disk) == strip_action_pins(canonical):
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
                f"Run `{_CLI_CMD} --resync` to re-render it from the "
                "canonical, reusing the app-name/app-image-name/enable-e2e/"
                "services-script values read back off this file. Any other hand "
                "edit is replaced (kept as tests.yaml.bak). If --resync reports "
                "it skipped (no parseable app-name, so its identity can't be "
                "read back), it needs a manual fix. " + _warn_only
            ),
        )
    ]

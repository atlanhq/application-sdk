"""C002 BootstrapWorkflowDrift — detect managed CI workflows that are absent or drifted.

The ``atlan-application-sdk-conformance bootstrap`` command installs a standard
set of CI workflow shims.  This check flags any managed file that is:

- missing (never bootstrapped, or accidentally deleted),
- structurally drifted from what ``bootstrap`` would write, or
- retired (``RETIRED_WORKFLOWS``) and still on disk — a shim bootstrap once
  installed fleet-wide and now removes, which until it does keeps firing on
  every PR with nothing behind it.

For parameterised templates the per-repo custom values are *extracted from the
on-disk file* before comparing, so intentional per-repo choices (app-name,
unit_tests_workflow, etc.) are not flagged as drift — only structural changes
are caught.

Two of those choices are app-owned opt-*ups* rather than plain identity values,
and are allowed for that reason:

- ``tests.yaml``'s ``unit-coverage-fail-under`` — an app raising its unit-test
  coverage floor above the SDK's own (``SDK_UNIT_COVERAGE_FLOOR``). Allowed at or
  above that floor; a value below it stays drift, since that would use the app's
  own workflow to duck under a fleet-wide bar.
- ``build-and-publish.yaml``'s ``use_ghcr_base`` — an app self-selecting the GHCR
  base-image redirect ahead of the SDK-side default flipping, which will be a
  long time coming for the whole fleet.

Neither should make a connector look non-conformant in drift reporting for doing
the better thing, which is what flagging them amounted to.

**Two drift tracks:**

1. **Managed shims** (``MANAGED_WORKFLOWS``): always-overwrite.
   Absent or drifted → WARN finding; run bootstrap to re-sync (re-runs
   overwrite). A ``RETIRED_WORKFLOWS`` name still present is the same track in
   reverse: WARN, and the same bare re-run deletes it.

2. **tests.yaml / renovate.json**: write-if-absent scaffolds.  Bootstrap
   creates each once and never clobbers customisations.  Drift is also tracked
   at WARN, never BLOCK.  Remediation: re-run bootstrap with ``--resync``,
   which re-renders them from the canonical while reusing the same values
   extracted here — so the structural catch-up lands and the per-repo choices
   survive.

   "The per-repo choices survive" only holds for choices the extractor reads
   back, which is what made FND-604: ``tests.yaml``'s ``force-external-runtime``
   and its explicit ``secrets:`` mapping were live inputs of the reusable that
   nothing read, so every ``--resync`` deleted them and CI reddened two tiers
   later with what looked like a source-system credential error.  Both are read
   back now, and ``unpreserved_tests_yaml_declarations`` covers the general
   case: a file declaring anything the canonical has no place for makes
   ``--resync`` refuse, and ``_unpreservable_note`` says so in this finding so
   the remediation this message recommends cannot silently decline.

Remediation: run ``atlan-application-sdk-conformance bootstrap`` to re-sync.
"""

from __future__ import annotations

from pathlib import Path

# The coverage floor is read as a module attribute (``bootstrap_extract.
# SDK_UNIT_COVERAGE_FLOOR``) rather than imported by value: a `from ... import`
# copy is fixed at import time, which would leave this checker's explanation of
# a sub-floor value quoting a different floor than the extractor actually
# applied whenever the constant is moved for a test — and the sub-floor branch
# can only be exercised by moving it, since the real floor is 0 and nothing can
# sit below it yet.
from conformance.bootstrap import extract as bootstrap_extract
from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_apt_packages,
    extract_field,
    extract_renovate_automerge,
    extract_tests_yaml_params,
    extract_use_ghcr_base,
    format_dropped_declarations,
    resolve_renovate_fallback_exit_zero,
    strip_action_pins,
    unpreserved_tests_yaml_declarations,
)
from conformance.bootstrap.render import (
    MANAGED_ACTION_FILES,
    MANAGED_WORKFLOWS,
    RETIRED_WORKFLOWS,
    render,
)
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
    # Retired shims: bootstrap installed these once and now deletes them, so a
    # copy still on disk is drift in the other direction — the file is reported
    # until the repo re-runs bootstrap (which removes it).
    paths.extend(wf_dir / name for name in RETIRED_WORKFLOWS)
    # Non-workflow vendored files (composite action + arg-building script).
    paths.extend(root / dest_rel for dest_rel in _MANAGED_ACTION_FILES_BY_DEST)
    # Write-if-absent scaffolds (WARN-only drift tracking).
    paths.append(wf_dir / _TESTS_WORKFLOW)
    paths.append(root / _RENOVATE_JSON)
    return paths


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Return C002 findings for *path* (may or may not exist on disk)."""
    if path.name in RETIRED_WORKFLOWS:
        return _scan_retired_shim(path, root)
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


def _scan_retired_shim(path: Path, root: Path) -> list[Finding]:
    """Flag a retired managed shim that is still on disk.

    The inverse of the absent-file finding for a managed shim: bootstrap wrote
    these into every consumer repo and now deletes them, so a surviving copy is
    a workflow still firing on every PR with nothing behind it. Remediation is
    the same bare re-run, which removes the file.
    """
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    if not path.exists():
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            file=rel,
            line=1,
            column=1,
            message=(
                f"CI workflow '{path.name}' was retired and is no longer managed, "
                f"but is still present. Run `{_CLI_CMD}` to remove it."
            ),
        )
    ]


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
    if name == "build-and-publish.yaml":
        kwargs["unit_tests_workflow"] = _extract_unit_tests_workflow(on_disk)
        # The GHCR base redirect is opt-in per app while the SDK-side default is
        # still false, and the fleet won't be ready to flip that default for a
        # long time. So an app that self-selects it is making a per-repo value
        # choice like any other here — not drift. Without this the opt-in reads
        # as a permanent C002 finding whose only "fix" (re-run bootstrap) sends
        # the app back to Harbor.
        kwargs["use_ghcr_base"] = extract_use_ghcr_base(on_disk)
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
    # legitimate param choices (app-name, enable-e2e, services-script, a
    # unit-coverage floor at or above the SDK's) are not.
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
                "services-script/unit-coverage-fail-under/force-external-runtime "
                "values and any explicit `secrets:` mapping read back off this "
                "file. Any other hand edit is replaced (kept as tests.yaml.bak). "
                "If --resync reports it skipped (no parseable app-name, so its "
                "identity can't be read back), it needs a manual fix. "
                + _unpreservable_note(on_disk, canonical)
                + _below_floor_coverage_note(on_disk)
                + _warn_only
            ),
        )
    ]


def _unpreservable_note(on_disk: str, canonical: str) -> str:
    """Return an explanation when *on_disk* declares keys the canonical drops.

    The counterpart to ``--resync``'s own refusal (FND-604): this checker is
    where a fleet-wide scan reports the file, so the reason the suggested
    remediation will decline to run belongs in the finding too — otherwise the
    message above promises a re-render that then does not happen, and the app
    owner has to run the command to find out why.

    Named here rather than resolved silently because these keys are exactly the
    per-repo CI wiring two prior incidents lost (FND-65, FND-110): an explicit
    ``secrets:`` mapping composing ``E2E_SOURCE_ENV_JSON``, a
    ``force-external-runtime: true``, a hand-kept extra job whose name branch
    protection still requires. Both of the first two are now preserved, so
    anything reaching this note is a third thing nobody has anticipated.
    """
    dropped = unpreserved_tests_yaml_declarations(on_disk, canonical)
    if not dropped:
        return ""
    return (
        f"Note: this file declares {format_dropped_declarations(dropped)}, which "
        "the canonical template has no place for, so a re-render would delete "
        "them — --resync therefore "
        "REFUSES on this file and leaves it untouched rather than dropping "
        "per-repo CI wiring silently. Reapply the structural update by hand, or "
        "remove those declarations first. "
    )


def _below_floor_coverage_note(on_disk: str) -> str:
    """Return an explanation when *on_disk* declares a sub-floor coverage value.

    A per-app ``unit-coverage-fail-under`` at or above ``SDK_UNIT_COVERAGE_FLOOR``
    is preserved and never reaches this message. One *below* it is the single
    param value this checker deliberately refuses to preserve, which makes it
    the one case where the generic "structural drift, run --resync" text is
    actively misleading: nothing about the file's structure is wrong, and
    ``--resync`` will resolve the finding by deleting the app's own line. Name
    it here so that outcome is a stated consequence rather than a surprise.
    """
    declared = bootstrap_extract.rejected_unit_coverage_fail_under(on_disk)
    if not declared:
        return ""
    return (
        f"Note: this file's `unit-coverage-fail-under: {declared}` is BELOW the "
        "SDK's own floor of "
        f"{bootstrap_extract.SDK_UNIT_COVERAGE_FLOOR}, so it is not preserved — "
        "apps may raise their unit-coverage floor above the SDK's, not duck under "
        "it. --resync therefore drops that line and the app inherits the SDK "
        "floor; raise the value to at or above the floor instead if the intent "
        "was an app-specific bar. "
    )

"""Bootstrap flag autodetection — read a repo's existing managed files back to
reproduce their current customization instead of resetting it to a default.

Split out of ``conformance.bootstrap.command`` (which was the single
most-churned file in its own package) so the "read an existing rendered file
to infer a flag value" concern lives in one focused module, separate from
argv parsing and the actual write phase.
"""

from __future__ import annotations

import pathlib

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_field,
    resolve_renovate_fallback_exit_zero,
)


def _read_workflow_field(path: pathlib.Path, field: str) -> str:
    """Return the value of ``field: <value>`` in *path*, or ``""``.

    Delegates to ``conformance.bootstrap.extract``'s ``extract_field`` (the
    single source of truth also used by the C002 checker's "read a rendered
    param back off disk" extraction) so this autodetection path and C002's
    drift-comparison extraction can never silently diverge on how a rendered
    field is parsed.
    """
    if not path.exists():
        return ""
    try:
        return extract_field(path.read_text(encoding="utf-8"), field)
    except (OSError, UnicodeDecodeError):
        return ""


def _read_atlan_yaml_name(root: pathlib.Path) -> str:
    """Return the ``name:`` value from ``atlan.yaml`` in *root*, or ``""``."""
    return _read_workflow_field(root / "atlan.yaml", "name")


def derive_app_name_from_dir(root: pathlib.Path) -> str:
    """Derive app name from the repo directory name.

    Strips a leading ``atlan-`` prefix and a trailing ``-app`` suffix so that
    e.g. ``atlan-openapi-app`` → ``openapi``.  Falls back to ``"app"`` if the
    result would be empty.
    """
    name = root.name
    if name.startswith("atlan-"):
        name = name[len("atlan-") :]
    if name.endswith("-app"):
        name = name[: -len("-app")]
    return name or "app"


def _read_enforce_from_renovate(root: pathlib.Path) -> str:
    """Fall back to *root*'s on-disk ``renovate.json`` enforcement signal.

    Used only when ``conformance.yaml``'s ``exit-zero`` line can't be read
    (see ``_read_conformance_enforce``) — ``renovate.json``'s own
    ``lockFileMaintenance`` block (soft mode) or absence thereof (hard mode)
    is a second, independent signal of the repo's actual enforcement mode.
    The automerge-to-exit-zero decision itself is
    ``resolve_renovate_fallback_exit_zero`` (the same one the C002 checker
    uses); this just inverts its polarity to the ``--enforce`` value this
    function returns.
    """
    renovate = root / "renovate.json"
    if not renovate.exists():
        return ""
    try:
        renovate_text = renovate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    exit_zero = resolve_renovate_fallback_exit_zero(renovate_text)
    return "false" if exit_zero == "true" else "true"


def _read_conformance_enforce(path: pathlib.Path, root: pathlib.Path) -> str:
    """Return the ``--enforce`` value that reproduces this repo's existing
    enforcement mode, or ``""`` if there is truly nothing to detect.

    Primary signal is *path*'s (``conformance.yaml``) ``exit-zero`` line. The
    rendered line is ``exit-zero: ${{ ... || << exit_zero >> }}`` — the
    boolean is the last token before the closing ``}}``, not the first token
    after ``exit-zero:`` (unlike the other managed-file fields), so this
    can't reuse ``_read_workflow_field``. ``exit-zero: true`` is soft/observe
    mode (``--enforce false``); ``exit-zero: false`` is hard-gate
    (``--enforce true``).

    Reuses ``EXIT_ZERO_RE`` from ``conformance.bootstrap.extract`` as the
    single source of truth for the pattern, rather than re-declaring an
    identical regex here.

    If *path* is absent, there is nothing to detect and this returns ``""``
    (falls through to the hard-gate default at derivation time) — that's the
    normal first-bootstrap case. But if *path* exists and its ``exit-zero``
    line simply doesn't match the expected pattern (hand-edited, or rendered
    by an older bootstrap template), silently falling through to the same
    hard-gate default would flip an intentionally soft-mode repo to hard-gate
    on a bare re-run — while ``renovate.json`` (which a bare re-run never
    force-overwrites) stays in its original soft-mode content, leaving the
    two managed files in different enforcement modes. Fall back to
    ``renovate.json``'s own on-disk signal instead of guessing hard-gate.
    """
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = EXIT_ZERO_RE.search(line)
            if m:
                return "false" if m.group(1) == "true" else "true"
    except (OSError, UnicodeDecodeError):
        return ""
    print(
        f"warning: {path} exists but its exit-zero line is unparseable"
        " -- falling back to renovate.json's enforcement signal"
    )
    return _read_enforce_from_renovate(root)


def apply_bootstrap_autodetection(kwargs: dict[str, str], root: pathlib.Path) -> None:
    """Fill in any bootstrap flag left unset (``""``) with its auto-detected value.

    Each flag's auto-detection reads back an existing managed file so that
    re-running ``bootstrap`` with no explicit flags reuses a repo's current
    customization instead of resetting it to the hardcoded default.
    """
    # package-name: existing docstring-coverage.yaml, else "app".
    if not kwargs["package_name"]:
        kwargs["package_name"] = (
            _read_workflow_field(
                root / ".github" / "workflows" / "docstring-coverage.yaml",
                "package_name",
            )
            or "app"
        )
    # unit-tests-workflow: existing build-and-publish.yaml, else "tests.yaml".
    if not kwargs["unit_tests_workflow"]:
        kwargs["unit_tests_workflow"] = (
            _read_workflow_field(
                root / ".github" / "workflows" / "build-and-publish.yaml",
                "unit_tests_workflow_file",
            )
            or "tests.yaml"
        )
    # app-name: atlan.yaml `name:` field, else the repo directory name.
    if not kwargs["app_name"]:
        kwargs["app_name"] = _read_atlan_yaml_name(root) or derive_app_name_from_dir(
            root
        )
    # services-script: existing .github/test/setup-services.sh, else unset.
    if not kwargs["services_script"]:
        candidate = root / ".github" / "test" / "setup-services.sh"
        if candidate.exists():
            kwargs["services_script"] = ".github/test/setup-services.sh"
    # enforce: existing conformance.yaml's exit-zero mode, else unset (falls
    # through to the hard-gate default at derivation time). Deliberately does
    # NOT affect force_renovate in main() -- renovate.json is only
    # force-overwritten when --enforce was passed explicitly on this
    # invocation, not when it was merely auto-detected.
    if not kwargs["enforce"]:
        kwargs["enforce"] = _read_conformance_enforce(
            root / ".github" / "workflows" / "conformance.yaml", root
        )

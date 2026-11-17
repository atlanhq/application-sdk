"""Bootstrap flag autodetection — read a repo's existing managed files back to
reproduce their current customization instead of resetting it to a default.

Split out of ``conformance.bootstrap.command`` (which was the single
most-churned file in its own package) so the "read an existing rendered file
to infer a flag value" concern lives in one focused module, separate from
argv parsing and the actual write phase.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    certify_apt_packages,
    extract_apt_packages,
    extract_build_publish_apt_packages,
    extract_build_publish_lfs,
    extract_build_publish_private_git_auth,
    extract_checks_private_git_deps,
    extract_conformance_private_git_deps,
    extract_field,
    extract_release_private_git_auth,
    extract_sarif_upload,
    extract_use_ghcr_base,
    extract_vulnerability_scan_lfs,
    resolve_renovate_fallback_exit_zero,
    sanitize_package_list,
)


def _read_with(path: pathlib.Path, extractor: Callable[[str], str]) -> str:
    """Return ``extractor(<text of path>)``, or ``""`` if *path* is missing or
    unreadable.

    Every extractor passed here comes from ``conformance.bootstrap.extract`` and
    is the same one the C002 drift checker uses, so a value detected here is
    re-rendered byte-identically and can't read as drift.
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return extractor(text)


def _read_workflow_field(path: pathlib.Path, field: str) -> str:
    """Return the value of ``field: <value>`` in *path*, or ``""``."""
    return _read_with(path, lambda text: extract_field(text, field))


def _ci_system_deps(text: str) -> str:
    """Normalise ``ci-system-deps.txt`` (a whitespace-separated package list).

    Goes through the same package-name filter as the checks.yml extraction, so
    a hand-edited file can't inject anything into the rendered workflow step.
    """
    return " ".join(sanitize_package_list(text))


_WORKFLOWS = pathlib.Path(".github") / "workflows"

# Flags whose value is read back from one always-overwrite managed workflow:
# (kwarg, path relative to the repo root, extractor). Without the read-back a
# bare bootstrap re-run deletes the per-repo choice the file carries.
#
# * use_ghcr_base: an app that opted into the GHCR base redirect would silently
#   revert to Harbor. The SDK default stays false until the fleet has soaked,
#   so opting in early has to be an app-owned, drift-free choice.
# * sarif_upload (FND-3336): keyed on the opt-in marker, not on the file
#   existing. Every repo bootstrapped before the opt-in holds an unmarked
#   copy, and on private repos that is exactly the copy this run must remove.
# * vuln_scan_lfs: an app that vendors LFS-tracked assets into its Docker build
#   context (a parser jar, a native lib) needs it, or the scan's image build
#   copies the pointer file. There is no unpreserved_declarations guard on
#   this file, so the line would just disappear on every bootstrap invocation,
#   including granular ones that have nothing to do with it.
# * build_publish_lfs: the same opt-in on the RELEASE image build. The gap only
#   bites a `release` event, so every PR stays green while the release path is
#   broken. One connector lost the line to a conformance rollout and did not
#   find out until a version could not be published.
# * conformance_private_git_deps / release_private_git_auth: a repo pinning a
#   private atlanhq package via ssh:// needs `private-git-deps` (plus the
#   `secrets: inherit` that feeds it ORG_PAT_GITHUB) for its D-series leg to
#   resolve, and `private_git_auth` for the release bump's `uv lock`. Both were
#   deleted on every bootstrap run. The first failure is loud (a REQUIRED
#   Conformance Gate goes red on a cold uv cache); the second is silent until
#   a release simply never happens.
# * build_publish_private_git_auth / checks_private_git_deps: the other two
#   places the same private dep is resolved: certify's `uv sync` in
#   build-and-publish.yaml, and setup-deps' `uv sync` ahead of pre-commit in
#   checks.yml.
_WORKFLOW_FLAGS: tuple[tuple[str, pathlib.Path, Callable[[str], str]], ...] = (
    ("use_ghcr_base", _WORKFLOWS / "build-and-publish.yaml", extract_use_ghcr_base),
    (
        "sarif_upload",
        _WORKFLOWS / "conformance-upload-sarif.yaml",
        extract_sarif_upload,
    ),
    (
        "vuln_scan_lfs",
        _WORKFLOWS / "vulnerability-scan.yml",
        extract_vulnerability_scan_lfs,
    ),
    (
        "build_publish_lfs",
        _WORKFLOWS / "build-and-publish.yaml",
        extract_build_publish_lfs,
    ),
    (
        "conformance_private_git_deps",
        _WORKFLOWS / "conformance.yaml",
        extract_conformance_private_git_deps,
    ),
    (
        "release_private_git_auth",
        _WORKFLOWS / "release.yaml",
        extract_release_private_git_auth,
    ),
    (
        "build_publish_private_git_auth",
        _WORKFLOWS / "build-and-publish.yaml",
        extract_build_publish_private_git_auth,
    ),
    (
        "checks_private_git_deps",
        _WORKFLOWS / "checks.yml",
        extract_checks_private_git_deps,
    ),
)


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
    return _read_with(
        root / "renovate.json",
        lambda text: (
            "false" if resolve_renovate_fallback_exit_zero(text) == "true" else "true"
        ),
    )


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
    # unit-tests-workflow: existing build-and-publish.yaml, else "tests.yaml".
    if not kwargs["unit_tests_workflow"]:
        kwargs["unit_tests_workflow"] = (
            _read_workflow_field(
                root / ".github" / "workflows" / "build-and-publish.yaml",
                "unit_tests_workflow_file",
            )
            or "tests.yaml"
        )
    # Per-repo opt-ins carried by always-overwrite workflows (see
    # _WORKFLOW_FLAGS for why each one has to be read back).
    for flag, relpath, extractor in _WORKFLOW_FLAGS:
        if not kwargs[flag]:
            kwargs[flag] = _read_with(root / relpath, extractor)
    # app-name: atlan.yaml `name:` field, else the repo directory name.
    if not kwargs["app_name"]:
        kwargs["app_name"] = _read_atlan_yaml_name(root) or derive_app_name_from_dir(
            root
        )
    # system-deps: the apt packages an existing checks.yml already
    # installs, else unset. checks.yml is always-overwrite, so without this a
    # bare re-run silently deletes a repo's build-header step (pykerberos et al
    # have no manylinux wheel) and its pre-commit job then fails on a cold
    # cache -- which is exactly what happened to the repos that hand-added the
    # step back after every bootstrap.
    # Falls back to .github/ci-system-deps.txt (the declaration the D-series
    # leg reads) so the value survives even if checks.yml was hand-stripped.
    if not kwargs["system_deps"]:
        kwargs["system_deps"] = _read_with(
            root / _WORKFLOWS / "checks.yml", extract_apt_packages
        ) or _read_with(root / ".github" / "ci-system-deps.txt", _ci_system_deps)
    # build-publish apt packages: the certify gate's `uv sync` builds the same
    # native extensions checks.yml and tests.yaml install headers for, so an
    # existing build-and-publish.yaml's `apt_packages` is kept, and a repo that
    # declares none there inherits its system-deps (read just above). Without
    # the fallback, a repo whose tests are green still cannot publish
    # ("Certify app": "Failed to build `pykerberos`"); without the read-back,
    # every bootstrap run deleted a hand-added line.
    if not kwargs["build_publish_apt_packages"]:
        kwargs["build_publish_apt_packages"] = (
            _read_with(
                root / _WORKFLOWS / "build-and-publish.yaml",
                extract_build_publish_apt_packages,
            )
            # system_deps' grammar is wider (apt-get takes pins, releases);
            # narrow it to what the reusable's certify validation accepts.
            or certify_apt_packages(kwargs["system_deps"])
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

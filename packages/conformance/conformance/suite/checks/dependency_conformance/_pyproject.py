"""Per-file pyproject.toml rules: D001, D002, D004–D009, D015–D018."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from conformance.suite.checks._ast_common import (
    iter_strings,
    parse_toml_suppressions,
    read_source,
)
from conformance.suite.schema.findings import Finding

from . import _metadata
from ._common import (
    _REMOTE_COMPONENT_FETCH_RE,
    DEPRECATED_SDK_EXTRAS,
    HATCHLING_BACKEND,
    PYRIGHT_WEAK_MODES,
    RULE_D001,
    RULE_D002,
    RULE_D004,
    RULE_D005,
    RULE_D006,
    RULE_D007,
    RULE_D008,
    RULE_D009,
    RULE_D016,
    RULE_D017,
    RULE_D018,
    RUNTIME_IMAGE_PYTHON,
    SDK_PACKAGE,
    SDK_PYTHON_FLOOR,
    TEST_ONLY_SDK_EXTRAS,
    _build_backend,
    _is_bounded_specifier,
    _is_self_check,
    _iter_dep_entries,
    _iter_dependency_group_entries,
    _line_of,
    _make_finding,
    _normalise_name,
    _parse_requirement,
    _poe_tasks,
    _project_name,
    _pyright_mode,
    _requires_python_excludes_runtime,
    _requires_python_lower_bound,
    _safe_load,
    _sdk_extras_in,
    _sdk_requirement_proves_pre_removal,
)


def scan_text(
    text: str,
    file: str,
    *,
    sdk_managed_packages: Iterable[str] | None = None,
    sdk_published_extras: Iterable[str] | None = None,
    test_imported_packages: Iterable[str] | None = None,
) -> list[Finding]:
    """Scan a pyproject.toml text and return D-series findings.

    Parameters
    ----------
    text:
        Raw pyproject.toml content.
    file:
        Repo-root-relative URI for findings.
    sdk_managed_packages:
        Override for the SDK's managed-deps set (test injection). When
        ``None``, looked up via ``importlib.metadata.requires``; when the SDK
        is not importable, D002/D004 are skipped silently.
    sdk_published_extras:
        Override for the SDK's published extras set (test injection). When
        ``None``, looked up via ``importlib.metadata`` ``Provides-Extra``; when
        the SDK is not importable, D005 is skipped silently.
    test_imported_packages:
        Distribution names directly imported by app-owned test sources. The
        full-repository scan supplies this context so D004 can distinguish a
        direct test dependency from redundant SDK-managed tooling.
    """
    data = _safe_load(text)
    if data is None:
        return []

    name = _project_name(text, data=data)
    if _is_self_check(name):
        return []

    findings: list[Finding] = []
    suppressions = parse_toml_suppressions(text)

    entries = list(_iter_dep_entries(text, data=data))

    # ── D001 ──────────────────────────────────────────────────────────────
    sdk_norm = _normalise_name(SDK_PACKAGE)
    project_deps = [e for e in entries if e.array_path == "project.dependencies"]
    sdk_entries = [e for e in project_deps if e.name == sdk_norm]

    if not sdk_entries:
        # Position the finding at the [project] table header (or line 1 if
        # we cannot find it) so reviewers know where the missing dep should go.
        anchor_line = 1
        for ln, line in enumerate(text.splitlines(), start=1):
            if line.strip() == "[project]":
                anchor_line = ln
                break
        findings.append(
            _make_finding(
                rule_id=RULE_D001,
                file=file,
                line=anchor_line,
                column=1,
                message=(
                    f"App pyproject.toml does not declare '{SDK_PACKAGE}' in "
                    f"[project.dependencies]. Every app must depend on the "
                    f"SDK with a bounded version specifier so fleet-wide "
                    f"upgrades remain reviewed and reproducible."
                ),
                suppressions=suppressions,
            )
        )
    else:
        for entry in sdk_entries:
            parsed = _parse_requirement(entry.raw)
            spec = parsed[1] if parsed else ""
            if not _is_bounded_specifier(spec):
                findings.append(
                    _make_finding(
                        rule_id=RULE_D001,
                        file=file,
                        line=entry.line,
                        column=entry.column,
                        message=(
                            f"'{SDK_PACKAGE}' is declared without a bounded "
                            f"version specifier (got '{entry.raw}'). Pin both "
                            f"a lower and an upper bound (e.g. "
                            f"'>=3.17.2,<4.0.0' or '~=3.17') so an automated "
                            f"SDK upgrade cannot pull in a future major "
                            f"version unreviewed."
                        ),
                        suppressions=suppressions,
                    )
                )

    # ── D006 ──────────────────────────────────────────────────────────────
    # Pure-text: runs regardless of whether the SDK metadata is importable, so
    # it must precede the D002 early-return below.
    py_bound = _requires_python_lower_bound(text, data=data)
    if py_bound is not None:
        (app_major, app_minor), app_op, py_line = py_bound
        if (app_major, app_minor) < SDK_PYTHON_FLOOR:
            sdk_major, sdk_minor = SDK_PYTHON_FLOOR
            findings.append(
                _make_finding(
                    rule_id=RULE_D006,
                    file=file,
                    line=py_line,
                    column=1,
                    message=(
                        f"App requires-python lower bound "
                        f"'{app_op}{app_major}.{app_minor}' is below the SDK's "
                        f"minimum supported Python ('>={sdk_major}.{sdk_minor}'). "
                        f"The app claims to support a Python the SDK does not; "
                        f"raise the lower bound to '>={sdk_major}.{sdk_minor}'."
                    ),
                    suppressions=suppressions,
                )
            )

    # ── D016 (pure-text) ────────────────────────────────────────────────────
    excluded = _requires_python_excludes_runtime(data)
    if excluded is not None:
        rt_major, rt_minor = RUNTIME_IMAGE_PYTHON
        findings.append(
            _make_finding(
                rule_id=RULE_D016,
                file=file,
                line=_line_of(text, "requires-python", section="project"),
                column=1,
                message=(
                    f"requires-python '{excluded}' does not admit every Python "
                    f"{rt_major}.{rt_minor}.x; app-runtime-base:3 ships "
                    f"{rt_major}.{rt_minor} (patch floats with the image) and the "
                    f"e2e harness runs it. Widen the range so the whole "
                    f"{rt_major}.{rt_minor} line is admitted (e.g. "
                    f"'<{rt_major}.{rt_minor + 1}', or drop the cap) and re-run "
                    f"`uv lock`."
                ),
                suppressions=suppressions,
            )
        )

    # ── D007 (pure-text) ────────────────────────────────────────────────────
    backend = _build_backend(text, data=data)
    if backend is not None and backend[0] != HATCHLING_BACKEND:
        findings.append(
            _make_finding(
                rule_id=RULE_D007,
                file=file,
                line=backend[1],
                column=1,
                message=(
                    f"Build backend '{backend[0]}' is non-standard for Atlan "
                    f"apps. Use Hatchling: set build-backend = "
                    f"'{HATCHLING_BACKEND}' and requires = ['hatchling'] in "
                    f"[build-system]."
                ),
                suppressions=suppressions,
            )
        )

    # ── D008 (pure-text) ────────────────────────────────────────────────────
    pyright = _pyright_mode(text, data=data)
    if pyright is not None and pyright[0].strip().lower() in PYRIGHT_WEAK_MODES:
        findings.append(
            _make_finding(
                rule_id=RULE_D008,
                file=file,
                line=pyright[1],
                column=1,
                message=(
                    f"pyright typeCheckingMode '{pyright[0]}' is weaker than "
                    f"the SDK baseline 'standard'. Raise it to at least "
                    f"'standard' so type regressions are caught in app CI."
                ),
                suppressions=suppressions,
            )
        )

    # ── D009 (pure-text) ────────────────────────────────────────────────────
    # Structural check (via `data`) just decides whether any poe task fetches
    # components remotely; the actual findings are anchored by a single
    # line-based scan of the raw text so a violation is never mis-attributed
    # to the wrong task name when more than one task matches.
    tasks = _poe_tasks(data)
    if tasks is not None and any(
        _REMOTE_COMPONENT_FETCH_RE.search(s)
        for task_def in tasks.values()
        for s in iter_strings(task_def)
    ):
        for ln, line in enumerate(text.splitlines(), start=1):
            if not _REMOTE_COMPONENT_FETCH_RE.search(line):
                continue
            findings.append(
                _make_finding(
                    rule_id=RULE_D009,
                    file=file,
                    line=ln,
                    column=1,
                    message=(
                        "A poe task fetches Dapr component YAMLs from GitHub "
                        f"over the network instead of reading them from the "
                        f"installed '{SDK_PACKAGE}' wheel, which bundles them "
                        f"at application_sdk/components/. Unauthenticated "
                        f"GitHub requests hit rate limits under CI "
                        f"concurrency, and a hardcoded ref drifts from "
                        f"whatever SDK version is actually locked in "
                        f"uv.lock. Copy from the installed package instead, "
                        f'e.g. `python -c "import application_sdk, pathlib, '
                        f"shutil; shutil.copytree(pathlib.Path("
                        f"application_sdk.__file__).parent / 'components', "
                        f"'components', dirs_exist_ok=True)\"`."
                    ),
                    suppressions=suppressions,
                )
            )

    # ── D002 / D004: redeclaration of SDK-managed core deps (metadata) ──────
    if sdk_managed_packages is None:
        managed = _metadata._sdk_managed_packages()
    else:
        managed = {_normalise_name(p) for p in sdk_managed_packages}

    group_entries = list(_iter_dependency_group_entries(text, data=data))
    test_imported = {
        _normalise_name(package) for package in (test_imported_packages or ())
    }

    if managed is not None:
        # D002 — [project.dependencies] + [project.optional-dependencies.*]
        for entry in entries:
            if entry.name == sdk_norm:
                continue  # the SDK itself is D001's concern
            if entry.name in managed:
                findings.append(
                    _make_finding(
                        rule_id=RULE_D002,
                        file=file,
                        line=entry.line,
                        column=entry.column,
                        message=(
                            f"'{entry.name}' is already pinned by "
                            f"'{SDK_PACKAGE}' and must not be redeclared in "
                            f"the app's [{entry.array_path}]. Remove the line; "
                            f"the SDK's contract will install it transitively."
                        ),
                        suppressions=suppressions,
                    )
                )
        # D004 — [dependency-groups.*] (dev/test groups; D002's coverage gap)
        for entry in group_entries:
            if entry.name == sdk_norm:
                continue
            if entry.name in managed and entry.name not in test_imported:
                findings.append(
                    _make_finding(
                        rule_id=RULE_D004,
                        file=file,
                        line=entry.line,
                        column=entry.column,
                        message=(
                            f"'{entry.name}' is pinned by '{SDK_PACKAGE}' and "
                            f"redeclared in the app's [{entry.array_path}]. "
                            f"Dev/test groups that re-pin an SDK-managed "
                            f"package drift from the SDK's dev environment and "
                            f"must be touched on every SDK bump. Remove the "
                            f"line, or pull it in via 'atlan-application-sdk"
                            f"[tests]'."
                        ),
                        suppressions=suppressions,
                    )
                )

    # ── D017: deprecated shim extra reference (pure-text) ────────────────────
    for entry in entries + group_entries:
        if entry.name != sdk_norm:
            continue
        parsed_requirement = _parse_requirement(entry.raw)
        sdk_specifier = parsed_requirement[1] if parsed_requirement is not None else ""
        for extra in _sdk_extras_in(entry.raw):
            shim = DEPRECATED_SDK_EXTRAS.get(_normalise_name(extra))
            if shim is None:
                continue
            if _sdk_requirement_proves_pre_removal(sdk_specifier, shim.removed_in):
                continue
            if shim.replacement is None:
                action = (
                    f"Its packages are core SDK dependencies, so remove "
                    f"'{extra}' from the extras list (and the brackets if it "
                    f"was the only one)."
                )
            else:
                action = (
                    f"Replace '{extra}' with '{shim.replacement}' (drop it if "
                    f"'{shim.replacement}' is already listed)."
                )
            findings.append(
                _make_finding(
                    rule_id=RULE_D017,
                    file=file,
                    line=entry.line,
                    column=entry.column,
                    message=(
                        f"'{SDK_PACKAGE}[{extra}]' in [{entry.array_path}] "
                        f"names a deprecated shim extra that installs nothing "
                        f"of its own and is removed in SDK v{shim.removed_in}. "
                        f"{action} Then run `uv lock`."
                    ),
                    suppressions=suppressions,
                    discriminator=_normalise_name(extra),
                )
            )

    # ── D018: test-only SDK extra in the runtime array (pure-text) ──────────
    # Only [project.dependencies] is graded: a production sync installs it and
    # skips both dependency groups and optional-dependencies arrays, so those
    # are where a test extra belongs.  Only the ``dev`` group counts as already
    # providing the extra: it is the one ``uv sync`` / ``uv run`` select by
    # default, so dropping the runtime reference is safe only when ``dev``
    # carries it.  Another group (``lint``, ``test``) may not be installed by
    # the app's test job, so it still gets the "add it to dev" guidance.
    dev_group_extras = {
        _normalise_name(extra)
        for entry in group_entries
        if entry.name == sdk_norm and entry.array_path == "dependency-groups.dev"
        for extra in _sdk_extras_in(entry.raw)
    }
    for entry in sdk_entries:
        for extra in _sdk_extras_in(entry.raw):
            norm_extra = _normalise_name(extra)
            installs = TEST_ONLY_SDK_EXTRAS.get(norm_extra)
            if installs is None:
                continue
            if norm_extra in dev_group_extras:
                group_action = (
                    f"[dependency-groups].dev already pulls in "
                    f"'{SDK_PACKAGE}[{extra}]', so only the runtime "
                    f"reference has to go."
                )
            else:
                group_action = (
                    f"Then add '{SDK_PACKAGE}[{extra}]' with the same version "
                    f"range to [dependency-groups].dev so tests still get it."
                )
            findings.append(
                _make_finding(
                    rule_id=RULE_D018,
                    file=file,
                    line=entry.line,
                    column=entry.column,
                    message=(
                        f"'{SDK_PACKAGE}[{extra}]' in [project.dependencies] "
                        f"installs the SDK's test tooling ({installs}) in the "
                        f"production image: a production sync installs "
                        f"[project.dependencies] and skips dependency groups. "
                        f"Remove '{extra}' from this requirement's extras "
                        f"(keep the version range and any other extras; drop "
                        f"the brackets if it was the only one). "
                        f"{group_action} Then run `uv lock`."
                    ),
                    suppressions=suppressions,
                    discriminator=norm_extra,
                )
            )

    # ── D005: invalid SDK extra reference (metadata) ────────────────────────
    if sdk_published_extras is None:
        published = _metadata._sdk_published_extras()
    else:
        published = {_normalise_name(e) for e in sdk_published_extras}

    if published is not None:
        for entry in entries + group_entries:
            if entry.name != sdk_norm:
                continue
            for extra in _sdk_extras_in(entry.raw):
                if _normalise_name(extra) not in published:
                    findings.append(
                        _make_finding(
                            rule_id=RULE_D005,
                            file=file,
                            line=entry.line,
                            column=entry.column,
                            message=(
                                f"'{SDK_PACKAGE}[{extra}]' in "
                                f"[{entry.array_path}] references an extra the "
                                f"SDK does not publish. uv silently drops an "
                                f"unknown extra, so its dependencies are never "
                                f"installed. Use a published extra or remove "
                                f"'{extra}'."
                            ),
                            suppressions=suppressions,
                        )
                    )

    return findings


def scan_path(
    path: Path,
    root: Path,
    *,
    sdk_managed_packages: Iterable[str] | None = None,
    sdk_published_extras: Iterable[str] | None = None,
    test_imported_packages: Iterable[str] | None = None,
) -> list[Finding]:
    """Scan a single pyproject.toml on disk."""
    source = read_source(path, root)
    if source is None:
        return []
    text, uri = source
    return scan_text(
        text,
        uri,
        sdk_managed_packages=sdk_managed_packages,
        sdk_published_extras=sdk_published_extras,
        test_imported_packages=test_imported_packages,
    )

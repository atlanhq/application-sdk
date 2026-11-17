"""D-series — pyproject.toml conformance against the application-sdk contract.

Rules in this check module:

* **D001 UnpinnedSdkDependency** — the app's ``[project.dependencies]`` array
  must declare ``atlan-application-sdk`` with a *bounded* version specifier
  (a lower bound *and* an upper bound, or a compatible-release ``~=``).
* **D002 RedeclaredSdkManagedDependency** — packages already pinned by the
  installed ``atlan-application-sdk`` distribution must not be redeclared in
  the app's ``[project.dependencies]`` or any
  ``[project.optional-dependencies.*]`` array.
* **D004 RedeclaredSdkManagedDependencyInGroups** — D002's coverage gap in PEP 735
  ``[dependency-groups]``; direct imports from app-owned test source are treated as
  app test dependencies rather than redundant SDK tooling pins.
* **D005 UnknownSdkExtra** — an ``atlan-application-sdk[extra]`` reference must
  name an extra the SDK actually publishes (uv silently drops unknown extras).
* **D006 IncompatibleRequiresPython** — the app's ``[project].requires-python``
  lower bound must not be below the SDK's minimum supported Python.
* **D016 RequiresPythonExcludesRuntimePython** — the app's
  ``[project].requires-python`` must admit the Python ``app-runtime-base:3``
  ships (3.13).
* **D007 NonStandardBuildBackend** — ``[build-system].build-backend`` must be
  Hatchling.
* **D008 WeakenedTypeChecking** — ``[tool.pyright].typeCheckingMode`` must not
  be weaker than the SDK baseline ``standard``.
* **D015 PyrightExcludeClobbersDefaults** — a ``[tool.pyright].exclude`` list
  replaces pyright's built-in defaults, so unless it restates the
  dot-directory protection (``**/.*``, or an explicit ``.venv`` entry) or the
  table scopes ``include``, a bare ``pyright`` run walks ``.venv``.
* **D009 RemoteDaprComponentFetch** — no ``[tool.poe.tasks.*]`` entry may fetch
  Dapr component YAMLs from ``raw.githubusercontent.com`` or the GitHub
  contents API for ``atlanhq/application-sdk``; the installed SDK wheel
  bundles them at ``application_sdk/components/``.
* **D010 QueryTransformerWithoutDuckdb** — an app that imports the SDK query
  transformer (``application_sdk.transformers.query``) must resolve
  ``duckdb`` on a *default* install: reachable from the app's own production
  dependencies in ``uv.lock`` when one exists (not merely present somewhere in
  that universal graph), else declared in ``[project] dependencies`` directly
  or via an ``atlan-application-sdk[sql]``/``[incremental]`` extra.  A missing
  duckdb is a guaranteed runtime ``ImportError`` in every transform.  The
  shape this describes is a plain SDK pin with no extra; the ``[daft]``
  population that surfaced it was an SDK defect (the extra resolved empty over
  3.22–3.27) and is fixed at the root in 3.28.0, where ``[daft]`` aliases
  ``[sql]`` again.

* **D017 DeprecatedSdkExtra** — an ``atlan-application-sdk[extra]``
  reference whose version specifier admits a release where the SDK removes a
  deprecated shim extra (v4.0.0 or later).

* **D018 SdkTestExtraInRuntimeDependencies** — an
  ``atlan-application-sdk[...]`` requirement in ``[project.dependencies]``
  names a test-only SDK extra (``[tests]``, ``[harness]``), so a production
  sync installs the SDK's test tooling in the runtime image.

* **D012 UnpinnedPackageIndex** — the repo's root ``pyproject.toml`` must pin
  PyPI as the resolver's default index (``[[tool.uv.index]]`` with
  ``default = true``).  Without it a machine-wide default index — the Endor
  Labs package firewall Atlan IT installs into ``~/.config/uv/uv.toml`` — is
  inherited, and every ``uv`` command silently rewrites every URL in
  ``uv.lock`` to that proxy.
* **D013 NonPyPILockfileIndex** — every *artifact* download URL in
  ``uv.lock`` (``sdist`` / ``wheels``) must name a PyPI host, and none may
  embed an index credential.  Direct ``source = { url = ... }`` archives are
  not an index rewrite and are not graded.  This is D012's damage once it has
  been committed: CI holds no credential for the proxy and fails at
  dependency install with ``401 Unauthorized``.

D004/D005 are metadata-based (need the SDK importable) like D002;
D006/D007/D008/D009/D017/D018 are pure-text.  D010 is cross-file (source imports + lock/pyproject) and runs in
``scan_all``.

Self-check exemption: any pyproject whose ``[project].name`` starts with
``atlan-application-sdk`` is skipped entirely (the SDK and its sibling packages
are *publishers* of the contract, not apps subject to it).

D012/D013 are ``scope=both`` and repo-level: they run in :func:`scan_all`
against the *root* ``pyproject.toml`` only, outside the self-check guard above,
because the SDK inherits a machine-wide index exactly as an app does.

Layout
------
One module per rule family; this package re-exports the public surface.

* ``_common`` — rule ids, constants, requirement / pyproject parsing.
* ``_metadata`` — installed-distribution readers; callers go through the module
  so a test patches one attribute.
* ``_pyproject`` — per-file rules (:func:`scan_text`, :func:`scan_path`).
* ``_duckdb`` (D010), ``_unused`` (D003), ``_conformance_dep`` (D011),
  ``_index`` (D012/D013/D014), ``_pyright`` (D015) — the repo-level rules
  :func:`scan_all` runs.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

from conformance.suite.checks._ast_common import (
    SuppressionsMap,
    _is_suppressed,
    is_sdk_package_name,
    make_cli_main,
    parse_toml_suppressions,
    safe_read_text,
)
from conformance.suite.checks._ast_common import discover as _discover_sources
from conformance.suite.checks._ast_common._discovery import (
    EXCLUDE_DIRS as _SOURCE_EXCLUDE_DIRS,
)
from conformance.suite.schema.findings import Finding

from . import _metadata
from ._common import (
    CONFORMANCE_PACKAGE,
    DEPRECATED_SDK_EXTRAS,
    HATCHLING_BACKEND,
    PYPI_LOCK_HOSTS,
    PYPI_SIMPLE_URL,
    PYRIGHT_WEAK_MODES,
    RULE_D001,
    RULE_D002,
    RULE_D003,
    RULE_D004,
    RULE_D005,
    RULE_D006,
    RULE_D007,
    RULE_D008,
    RULE_D009,
    RULE_D010,
    RULE_D011,
    RULE_D012,
    RULE_D013,
    RULE_D014,
    RULE_D015,
    RULE_D016,
    RULE_D017,
    RULE_D018,
    RUNTIME_IMAGE_PYTHON,
    SDK_PACKAGE,
    SDK_PYTHON_FLOOR,
    SERIES,
    TEST_ONLY_SDK_EXTRAS,
    DeprecatedSdkExtra,
    _is_self_check,
    _iter_dep_entries,
    _normalise_name,
    _project_name,
)
from ._conformance_dep import (
    _scan_conformance_dependency,
)
from ._duckdb import (
    _scan_query_transformer_duckdb,
)
from ._index import (
    _scan_default_index,
    _scan_lockfile_index,
    _scan_resolver_fence,
)
from ._pyproject import scan_path, scan_text
from ._pyright import (
    PYRIGHT_DEFAULT_EXCLUDES,
    _scan_pyright_excludes,
)
from ._unused import (
    _collect_source_usage,
    _discover_test_sources,
    _is_app_source,
    _is_test_source,
    _owning_pyproject,
    _scan_constraint_floors,
    _scan_unused_dependencies,
    _test_imported_distributions,
)

__all__ = [
    "CONFORMANCE_PACKAGE",
    "DEPRECATED_SDK_EXTRAS",
    "DeprecatedSdkExtra",
    "HATCHLING_BACKEND",
    "PYPI_LOCK_HOSTS",
    "PYPI_SIMPLE_URL",
    "PYRIGHT_DEFAULT_EXCLUDES",
    "PYRIGHT_WEAK_MODES",
    "RULE_D001",
    "RULE_D002",
    "RULE_D003",
    "RULE_D004",
    "RULE_D005",
    "RULE_D006",
    "RULE_D007",
    "RULE_D008",
    "RULE_D009",
    "RULE_D010",
    "RULE_D011",
    "RULE_D012",
    "RULE_D013",
    "RULE_D014",
    "RULE_D015",
    "RULE_D016",
    "RULE_D017",
    "RULE_D018",
    "RUNTIME_IMAGE_PYTHON",
    "SDK_PACKAGE",
    "SDK_PYTHON_FLOOR",
    "SERIES",
    "TEST_ONLY_SDK_EXTRAS",
    "discover",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
    "SuppressionsMap",
    "_is_suppressed",
    "is_sdk_package_name",
    "safe_read_text",
    "_SOURCE_EXCLUDE_DIRS",
]


def scan_all(
    paths: list[Path],
    root: Path,
    *,
    dist_import_map: Mapping[str, set[str] | None] | None = None,
    imported_modules: set[str] | None = None,
    dialect_drivers: set[str] | None = None,
    dialect_names: set[str] | None = None,
    dialect_entry_points: Mapping[str, set[str]] | None = None,
) -> list[Finding]:
    """Run the full D-series over *paths*: per-file D001/D002 + cross-file D003.

    ``paths`` is the post-exclusion discovery list (the root ``pyproject.toml``
    plus the repo's Python sources — see :func:`discover`).

    D001/D002 run per ``pyproject.toml`` via :func:`scan_path` (the SDK and its
    sibling packages remain self-exempt). D004 also reads test-only Python
    sources separately: a package directly imported there is an app-owned test
    dependency, not a redundant SDK tooling re-pin. D003 is computed here
    *outside* that self-check guard — it is ``scope=both`` and so applies to the
    SDK itself — by mapping each core dependency to the import names it provides
    and flagging any whose modules never appear in the collected source imports.

    Test seams: ``dist_import_map`` injects the dependency -> import-name map
    (default: resolved from installed metadata) and ``imported_modules`` injects
    the set of imported top-levels (default: parsed from the discovered sources).
    ``dialect_names`` and ``dialect_entry_points`` inject the URL-scheme dialect
    names and each dependency's ``sqlalchemy.dialects`` entry points.
    """
    pyprojects = [p for p in paths if p.name == "pyproject.toml"]
    all_py_files = [p for p in paths if p.suffix == ".py"]
    py_files = [p for p in all_py_files if _is_app_source(p, root)]

    # D004 test context, scoped per project: a test file only vouches for the
    # nearest pyproject that contains it, and only files the caller passed in
    # (so runner ``--exclude`` prefixes and CLI scan dirs are honoured).
    tests_by_pyproject: dict[Path, list[Path]] = {}
    for test_file in (p for p in all_py_files if _is_test_source(p, root)):
        owner = _owning_pyproject(test_file, pyprojects)
        if owner is not None:
            tests_by_pyproject.setdefault(owner, []).append(test_file)

    findings: list[Finding] = []
    for pyproject in pyprojects:
        findings.extend(
            scan_path(
                pyproject,
                root,
                test_imported_packages=_test_imported_distributions(
                    tests_by_pyproject.get(pyproject, [])
                ),
            )
        )

    root_pyproject = root / "pyproject.toml"
    if not root_pyproject.is_file():
        return findings
    try:
        text = root_pyproject.read_text(encoding="utf-8")
        tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return findings

    try:
        rel_pyproject = str(root_pyproject.relative_to(root))
    except ValueError:
        rel_pyproject = str(root_pyproject)

    # ── D010 (cross-file; app-only like the rest of the per-pyproject rules) ─
    if not _is_self_check(_project_name(text)):
        findings.extend(
            _scan_query_transformer_duckdb(py_files, root, text, rel_pyproject)
        )
        # ── D003 constraint floors (app-only: the SDK is where floors live) ──
        findings.extend(_scan_constraint_floors(text, rel_pyproject))

    # ── D011 (repo-level: a property of the root pyproject, not of each) ────
    findings.extend(_scan_conformance_dependency(text, rel_pyproject, root))

    # ── D012 / D013 (repo-level, scope=both: computed outside the self-check
    # guard, because the SDK inherits a machine-wide index just as an app does)
    findings.extend(_scan_default_index(text, rel_pyproject))
    findings.extend(_scan_lockfile_index(root, text, rel_pyproject))

    # ── D014 (repo-level, scope=both: a resolver fence bounds the SDK's own
    # resolves exactly as it bounds an app's, /fix-vulnerabilities included) ──
    findings.extend(_scan_resolver_fence(text, rel_pyproject))

    # ── D015 (repo-level, scope=both: the SDK's own pyproject carries this
    # exact shape, and a bare pyright run is as slow there as in any app) ────
    findings.extend(_scan_pyright_excludes(text, rel_pyproject))

    # ── D003 ────────────────────────────────────────────────────────────────
    dep_entries = [
        e
        for e in _iter_dep_entries(text)
        if e.array_path == "project.dependencies"
        and e.name != _normalise_name(SDK_PACKAGE)
    ]
    if not dep_entries:
        return findings

    if imported_modules is None or dialect_drivers is None or dialect_names is None:
        source_imports, source_drivers, source_dialect_names = _collect_source_usage(
            py_files
        )
        if imported_modules is None:
            imported_modules = source_imports
        if dialect_drivers is None:
            dialect_drivers = source_drivers
        if dialect_names is None:
            dialect_names = source_dialect_names
    # Resolve against the repo under test first (see _repo_site_packages), so
    # the finding set belongs to the repo and not to whichever interpreter
    # happened to invoke the suite.
    site_packages = _metadata._repo_site_packages(root)
    need_imports = dist_import_map is None
    need_dialects = dialect_entry_points is None and bool(dialect_names)
    if need_imports or need_dialects:
        env_imports, env_dialects = _metadata._env_distribution_metadata(
            site_packages,
            include_imports=need_imports,
            include_dialects=need_dialects,
            dialect_names_for={_normalise_name(e.name) for e in dep_entries},
        )
    else:
        env_imports, env_dialects = {}, {}
    if dist_import_map is None:
        dist_import_map = {
            e.name: _metadata._dist_import_names(e.name, env_map=env_imports)
            for e in dep_entries
        }
    if dialect_entry_points is None:
        if dialect_names:
            dialect_entry_points = {
                e.name: _metadata._dist_dialect_entry_points(
                    e.name, env_map=env_dialects
                )
                for e in dep_entries
            }
        else:
            dialect_entry_points = {}

    try:
        rel = root_pyproject.relative_to(root)
    except ValueError:
        rel = root_pyproject

    d003_findings, unresolved = _scan_unused_dependencies(
        dep_entries,
        imported_modules,
        parse_toml_suppressions(text),
        str(rel),
        dist_import_map=dist_import_map,
        dialect_drivers=dialect_drivers,
        dialect_names=dialect_names,
        dialect_entry_points=dialect_entry_points,
    )
    findings.extend(d003_findings)

    if unresolved:
        # No silent caps: a dependency we cannot resolve in this environment is
        # not analysed; surface it (to stderr, so SARIF on stdout stays clean)
        # rather than letting "0 D003 findings" imply full coverage.
        noun = "dependency" if len(unresolved) == 1 else "dependencies"
        print(  # noqa: T201 — stderr diagnostic; stdout carries the SARIF report
            f"conformance (D003): skipped {len(unresolved)} {noun} not importable "
            f"in this environment (unused status undetermined): "
            f"{', '.join(sorted(unresolved))}",
            file=sys.stderr,
        )

    return findings


def discover(root: Path) -> list[Path]:
    """Return the root ``pyproject.toml`` (if any) plus the repo's Python sources.

    D001/D002 need the ``pyproject.toml``; D003 additionally needs every source
    file's imports.  Source discovery reuses the shared AST walk, which already
    excludes tests, build dirs, virtualenvs, and dot-directories.  Test sources
    are appended separately for D004's test-import context.
    """
    paths: list[Path] = []
    candidate = root / "pyproject.toml"
    if candidate.is_file():
        paths.append(candidate)
    paths.extend(_discover_sources(root))
    # Test sources feed D004 only; scan_all partitions them back out of D003.
    seen = set(paths)
    paths.extend(p for p in _discover_test_sources(root) if p not in seen)
    return paths


main = make_cli_main(
    scan_all=scan_all,
    description="D-series: scan a repo against the application-sdk dependency contract.",
    discover=discover,
    default_scan_paths=(".",),
)


"""CLI entry point for the D-series check."""


if __name__ == "__main__":
    sys.exit(main())

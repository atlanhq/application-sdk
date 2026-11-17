"""D010 — the SDK query transformer imported without ``duckdb`` on a default install."""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

from conformance.suite.checks._ast_common import (
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

from ._common import (
    RULE_D010,
    SDK_PACKAGE,
    _iter_dep_entries,
    _make_finding,
    _normalise_name,
    _project_name,
    _sdk_extras_in,
)

#: The SDK module whose import gates an app into D010: everything under it
#: (``transform_metadata`` / ``QueryBasedTransformer``) executes transform SQL
#: through ``DuckDBConnectionManager``.
_QUERY_TRANSFORMER_MODULE = "application_sdk.transformers.query"


#: The parent package, for the ``from application_sdk.transformers import
#: query`` form.
_TRANSFORMERS_PACKAGE = "application_sdk.transformers"


#: SDK extras that provide duckdb (per the SDK's published extras).  The
#: deprecated ``[daft]`` alias is left out on purpose: it resolved empty over
#: SDK 3.22–3.27, so only a lock can say whether it provides duckdb.
_DUCKDB_PROVIDING_EXTRAS = frozenset({"sql", "incremental"})


def _first_query_transformer_import(
    py_files: Iterable[Path], root: Path
) -> tuple[str, int] | None:
    """Return ``(rel_file, lineno)`` of the first query-transformer import.

    Matches ``import application_sdk.transformers.query`` (or a submodule),
    ``from application_sdk.transformers.query import ...``, and
    ``from application_sdk.transformers import query``.
    """
    for path in py_files:
        try:
            tree = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Import):
                hit = any(
                    alias.name == _QUERY_TRANSFORMER_MODULE
                    or alias.name.startswith(_QUERY_TRANSFORMER_MODULE + ".")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == _QUERY_TRANSFORMER_MODULE or node.module.startswith(
                    _QUERY_TRANSFORMER_MODULE + "."
                ):
                    hit = True
                elif node.module == _TRANSFORMERS_PACKAGE:
                    hit = any(alias.name == "query" for alias in node.names)
            if hit:
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    rel = str(path)
                return rel, node.lineno
    return None


#: Marker axes that describe the *machine*, and are therefore decidable from the
#: scanning host. Everything else — notably the ``python_version`` family — is
#: treated as non-decidable, because a lock resolved for the app's own
#: ``requires-python`` range says nothing about the interpreter running the scan.
_PLATFORM_MARKER_AXES = frozenset(
    {"sys_platform", "os_name", "platform_system", "platform_machine"}
)


_MARKER_VARIABLE_RE = re.compile(r"\b([a-z_]+)\s*(?:==|!=|<|>|<=|>=|~=|\bin\b)")


def _marker_can_hold(marker: object) -> bool:
    """Whether a PEP 508 environment marker on a lock edge can hold here.

    Markers gate whether an edge is installed at all, so ignoring them lets a
    platform-specific dependency read as universally reachable — a
    ``marker = "sys_platform == 'win32'"`` duckdb edge would silence D010 on
    Linux and macOS, where the ``ImportError`` it exists to catch is real.

    Only **platform** axes are evaluated, against the scanning host.  Markers
    touching ``python_version``/``python_full_version``/``implementation_name``
    are deliberately treated as non-decidable and left contributing: ``uv.lock``
    partitions its resolution on exactly those axes, so evaluating them against
    the scanning interpreter would report a fully compliant app whenever the
    scan host's Python differs from the app's declared target — the mirror image
    of the bug this function exists to fix.

    ``packaging`` is a declared dependency of this package; the defensive
    ``ImportError`` fallback remains so that a hand-run copy of the suite (or a
    broken environment) degrades to the prior "every marker contributes"
    behaviour rather than crashing the scan — and when the marker does not
    parse the edge is likewise treated as contributing rather than inventing a
    false positive.
    """
    if marker is None:
        return True
    text = str(marker)
    axes = set(_MARKER_VARIABLE_RE.findall(text))
    if not axes or not axes <= _PLATFORM_MARKER_AXES:
        return True  # non-decidable (or unrecognised): stay conservative
    try:
        from packaging.markers import InvalidMarker, Marker  # noqa: PLC0415 — guarded by the deliberate ImportError fallback (see docstring)
    except ImportError:
        return True
    try:
        return bool(Marker(text).evaluate())
    except (InvalidMarker, ValueError, KeyError):
        return True


def _locked_package_version(root: Path, name: str) -> str | None:
    """Return the version *name* resolves to in ``root/uv.lock``, else None.

    ``None`` means "not in the lock" — either the lock does not exist, cannot be
    parsed, or carries no such package.  Callers must not treat an unparseable
    lock as a violation; D011 only reports a *missing entry* when the lock is
    readable, so a malformed lock never manufactures a finding.
    """
    lock = root / "uv.lock"
    if not lock.is_file():
        return None
    try:
        doc = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    target = _normalise_name(name)
    for pkg in doc.get("package", []):
        if not isinstance(pkg, dict):
            continue
        if _normalise_name(str(pkg.get("name", ""))) == target:
            version = pkg.get("version")
            return str(version) if version is not None else None
    return None


def _lock_is_readable(root: Path) -> bool:
    """True iff ``root/uv.lock`` exists and parses — the D011 lock branch's gate."""
    lock = root / "uv.lock"
    if not lock.is_file():
        return False
    try:
        tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return True


def _lock_dependency_edges(
    pkg: dict[str, object], extras: frozenset[str]
) -> list[tuple[str, frozenset[str]]]:
    """Production dependency edges out of a locked package.

    Only ``dependencies`` plus the ``optional-dependencies`` groups actually
    activated by an incoming extra.  ``dev-dependencies`` and
    ``[package.metadata]`` are deliberately not traversed — they are not part of
    a default (``uv sync --no-dev``) install, which is the environment D010
    grades.
    """
    raw: list[object] = []
    deps = pkg.get("dependencies")
    if isinstance(deps, list):
        raw.extend(deps)
    optional = pkg.get("optional-dependencies")
    if isinstance(optional, dict):
        for extra in extras:
            group = optional.get(extra)
            if isinstance(group, list):
                raw.extend(group)

    out: list[tuple[str, frozenset[str]]] = []
    for dep in raw:
        if not isinstance(dep, dict):
            continue
        name = _normalise_name(str(dep.get("name", "")))
        if not name:
            continue
        if not _marker_can_hold(dep.get("marker")):
            continue  # e.g. a win32-only edge, unreachable on this install
        # uv uses ``extras`` for a dependency request carrying one or more
        # extras. Older lock shapes (and our hand-written fixtures) use the
        # singular ``extra`` key, so accept both without treating an
        # unactivated optional dependency as reachable.
        dep_extras = dep.get("extras", dep.get("extra"))
        activated = (
            frozenset(_normalise_name(str(e)) for e in dep_extras)
            if isinstance(dep_extras, list)
            else frozenset()
        )
        out.append((name, activated))
    return out


def _duckdb_in_lock(lock_text: str, pyproject_text: str) -> bool | None:
    """Whether duckdb is reachable from the app's own production dependencies.

    ``uv.lock`` is a *universal* resolution graph: it carries dev groups and
    every extra of every package, so the mere presence of a ``duckdb`` package
    block says nothing about whether a default install gets it.  This walks the
    graph from the app's own ``[[package]]`` entry along production edges only.

    Returns ``None`` when the lock cannot be parsed or the app's root entry
    cannot be identified — the caller then falls back to declaration intent.
    """
    try:
        lock = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError:
        return None
    packages = lock.get("package")
    if not isinstance(packages, list):
        return None

    by_name: dict[str, dict[str, object]] = {}
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        name = _normalise_name(str(pkg.get("name", "")))
        if name:
            by_name.setdefault(name, pkg)

    raw_app_name = _project_name(pyproject_text)
    if raw_app_name is None:
        return None
    app_name = _normalise_name(raw_app_name)
    if app_name not in by_name:
        return None

    seen: set[tuple[str, frozenset[str]]] = set()
    queue: list[tuple[str, frozenset[str]]] = [(app_name, frozenset())]
    while queue:
        name, extras = queue.pop()
        key = (name, extras)
        if key in seen:
            continue
        seen.add(key)
        if name == "duckdb":
            return True
        pkg = by_name.get(name)
        if pkg is None:
            continue
        queue.extend(_lock_dependency_edges(pkg, extras))
    return False


def _duckdb_resolved(root: Path, pyproject_text: str) -> bool:
    """Whether ``duckdb`` resolves for the repo at *root* on a default install.

    With a parseable ``uv.lock`` the lock is the ground truth, walked from the
    app's own package entry along production edges (see :func:`_duckdb_in_lock`)
    rather than searched flat — duckdb sitting in the graph because a dev-only
    tool pulls it in does NOT save a production ``uv sync --no-dev``.

    Without a usable lock, fall back to declaration intent in
    ``pyproject.toml``, restricted to ``[project] dependencies``: a direct
    ``duckdb`` dependency, or an ``atlan-application-sdk`` reference carrying a
    duckdb-providing extra (``sql``/``incremental``).  Dependency groups and
    optional-dependency arrays are excluded — they are not installed by default.
    """
    uv_lock = root / "uv.lock"
    if uv_lock.is_file():
        try:
            lock_text = uv_lock.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            lock_text = None
        if lock_text is not None:
            resolved = _duckdb_in_lock(lock_text, pyproject_text)
            if resolved is not None:
                return resolved
        # unreadable/unparseable lock, or an unidentifiable root package —
        # fall back to pyproject intent below.

    sdk_norm = _normalise_name(SDK_PACKAGE)
    for entry in _iter_dep_entries(pyproject_text):
        if entry.array_path != "project.dependencies":
            continue
        if entry.name == "duckdb":
            return True
        if entry.name == sdk_norm and _DUCKDB_PROVIDING_EXTRAS & {
            _normalise_name(e) for e in _sdk_extras_in(entry.raw)
        }:
            return True
    return False


def _scan_query_transformer_duckdb(
    py_files: list[Path],
    root: Path,
    pyproject_text: str,
    file: str,
) -> list[Finding]:
    """D010: query-transformer import present but duckdb does not resolve."""
    import_site = _first_query_transformer_import(py_files, root)
    if import_site is None:
        return []
    if _duckdb_resolved(root, pyproject_text):
        return []

    rel_import, import_line = import_site
    suppressions = parse_toml_suppressions(pyproject_text)
    # Anchor at the SDK dependency line (where the extra belongs) so the
    # finding lands where the fix goes; fall back to line 1 of pyproject.toml.
    sdk_norm = _normalise_name(SDK_PACKAGE)
    anchor_line = 1
    for entry in _iter_dep_entries(pyproject_text):
        if entry.name == sdk_norm and entry.array_path == "project.dependencies":
            anchor_line = entry.line
            break
    return [
        _make_finding(
            rule_id=RULE_D010,
            file=file,
            line=anchor_line,
            column=1,
            message=(
                f"The SDK query transformer is imported ({rel_import}:{import_line}"
                f" — application_sdk.transformers.query) but 'duckdb' does not "
                f"resolve from the app's production dependencies (uv.lock is a "
                f"universal graph — a dev-only or unactivated-extra duckdb does "
                f"not install by default) and no "
                f"'{SDK_PACKAGE}[sql]'/'[incremental]' extra or direct duckdb "
                f"dependency is declared. Every transform_metadata call then "
                f"fails at runtime with ImportError: 'duckdb is required for "
                f"DuckDBConnectionManager'. First confirm a live code path "
                f"imports the module at {rel_import}: if it is dead code "
                f"(nothing imports it), delete it instead of adding a "
                f"dependency; if the repo keeps it "
                f"on purpose (e.g. frozen reference code), suppress D010 on "
                f"this finding's pyproject.toml anchor line (pyproject.toml:"
                f"{anchor_line} — the SDK dependency line when [project] "
                f"dependencies declares one, else line 1). For a live import, "
                f"reference the SDK as "
                f"'{SDK_PACKAGE}[sql]' (or [incremental]) and relock; if the app "
                f"is pinned to the deprecated '{SDK_PACKAGE}[daft]' extra, upgrade "
                f"to SDK >= 3.28.0 instead — that extra resolved empty over "
                f"3.22–3.27 and aliases [sql] again from 3.28.0, so the bump is "
                f"the whole fix. Declaring duckdb directly also clears this "
                f"finding but duplicates a pin the SDK's extras manage; prefer "
                f"the extra."
            ),
            suppressions=suppressions,
        )
    ]

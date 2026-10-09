"""T026 E2EHarnessTenantPoolMismatch — e2e harness base vs declared app type.

Why this rule exists
--------------------
FND-3542 split the e2e tenants into two pools and made the harness enforce the
split at runtime: a suite inheriting ``SystemAppE2ETest`` refuses to run unless
``E2E_TENANT_POOL=system``, and every other suite refuses the ``system`` pool.
Which repos *see* the system tenants is decided by who can read the
``E2E_SYSTEM_TENANT_MATRIX_JSON`` secret.

That gate only engages once a suite picks a base, so two regressions remain that
only a static check can see:

1. A **system app** adopts the harness on plain ``BaseE2ETest`` (or its generated
   base), so it never opts into the system pool and runs on the connector
   tenants — the FND-438 failure mode, where a system app's test bed doubles as
   a connector's.
2. A **connector** adopts ``SystemAppE2ETest`` to claim the system pool.

The signal: the app's declared marketplace ``type``
---------------------------------------------------
``atlan.yaml`` (generated from ``contract/app.pkl``) declares the app's
top-level ``type``. It is compared case-insensitively, because the fleet carries
both ``System``/``system`` and ``Utility``/``utility``:

* ``system`` — every SDK-harness e2e test class **must** reach
  ``SystemAppE2ETest``.
* ``utility`` — either base is allowed. A few system apps (connection-delete,
  model-caster) keep ``utility`` because they have a marketplace tile and can be
  run directly, but only ever inside a tenant. For a utility, inheriting
  ``SystemAppE2ETest`` *is* the app's declaration, and once the repo is on the
  system secret the runtime gate holds it there in both directions. A utility
  that should be on the system pool but never adopts the base is
  indistinguishable from every other utility; that gap is accepted.
* ``connector`` — no SDK-harness e2e test class may reach ``SystemAppE2ETest``.
* any other type, or no ``atlan.yaml`` — the rule does not apply.

``type`` is read with a column-0 regex rather than a YAML parser, matching every
other ``atlan.yaml`` reader in this package (``sdr_test_checks``,
``app_name_alignment``, ``release_contract``): the package carries no YAML
dependency, and the toolkit-generated file always writes ``type:`` as a plain
top-level scalar. The indented ``type:`` lines under ``entrypoints:`` never
match.

What is graded
--------------
Every collectable test class (``Test*`` in a ``test_*.py`` / ``*_test.py``
file) under ``tests/e2e/`` that is an **SDK-harness class**: one that reaches
``BaseE2ETest``, ``SQLAppE2ETest`` or ``SystemAppE2ETest`` — directly, through a
repo-local base anywhere under ``tests/``, or through a toolkit-generated
``*GeneratedE2EBase`` in ``app/generated/**/_e2e_base.py`` (which for a
``type = "system"`` contract already extends ``SystemAppE2ETest``).

A base name counts as the SDK class only when it is imported from
``application_sdk.testing.e2e`` (or a submodule of it). A same-named class
imported from anywhere else is resolved against the repo's own classes, or
ignored when it is not one of them, so a local ``SystemAppE2ETest`` stand-in
cannot satisfy or trip the rule. A bare name with no import binding (a relative
or star import) is resolved against the repo's own classes by name; when that
name is defined in several files, every definition contributes, which errs
towards recognising ``SystemAppE2ETest`` — a false negative on ``system``, a
reportable (and suppressible) finding on ``connector``.

Inline suppression
------------------
``# conformance: ignore[T026] <reason>`` on the ``class`` line or the
comment-only line directly above it.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    collect_import_origins,
    is_collectable_test_file,
    is_test_class,
    make_cli_main,
    make_finding,
)
from conformance.suite.checks.e2e_generated_harness import _generated_base_files
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T026 = "T026"

_SDK_E2E_MODULE = "application_sdk.testing.e2e"
_SYSTEM_BASE = "SystemAppE2ETest"
# Every SDK e2e harness base. SystemAppE2ETest and SQLAppE2ETest both derive
# from BaseE2ETest, so reaching any of them makes a class an SDK-harness class.
_SDK_HARNESS_BASES: frozenset[str] = frozenset(
    {"BaseE2ETest", "SQLAppE2ETest", _SYSTEM_BASE}
)

_TYPE_SYSTEM = "system"
_TYPE_CONNECTOR = "connector"

# Column-0 anchored so the indented ``type:`` under ``entrypoints:`` never matches.
_ATLAN_YAML_TYPE_RE = re.compile(r"^type:[ \t]*(.*)$", re.MULTILINE)

_DOCS_LINK = "docs/standards/connector-ci-e2e.md#system-apps"

__all__ = ["RULE_T026", "SERIES", "discover", "main", "scan_all", "scan_path"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Walk ``tests/`` for Python sources.

    Only classes under ``tests/e2e/`` are graded, but a shared harness base can
    live anywhere under ``tests/``, so the whole tree is indexed for resolution.
    """
    base = root / "tests"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


# ---------------------------------------------------------------------------
# Declared type
# ---------------------------------------------------------------------------


def _declared_type(root: Path) -> str | None:
    """The top-level ``type`` in ``atlan.yaml`` as written, or None if absent."""
    try:
        text = (root / "atlan.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _ATLAN_YAML_TYPE_RE.search(text)
    if match is None:
        return None
    value = match.group(1).split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] in "\"'" and value[0] == value[-1]:
        value = value[1:-1].strip()
    return value or None


# ---------------------------------------------------------------------------
# Class index and base resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClassInfo:
    file: str
    node: ast.ClassDef


@dataclass
class _Index:
    #: ``(file, class name)`` -> class; a definition in the referencing file wins.
    classes: dict[tuple[str, str], _ClassInfo]
    #: bare class name -> every repo definition, for cross-file resolution.
    by_name: dict[str, list[_ClassInfo]]
    #: per-file ``{bound name: fully-qualified import origin}``.
    origins: dict[str, dict[str, str]]


def _index_file(path: Path, rel: str, index: _Index) -> tuple[ast.Module, str] | None:
    """Index *path*'s classes and imports; return ``(tree, source)`` if parseable."""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    index.origins[rel] = collect_import_origins(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            info = _ClassInfo(rel, node)
            index.classes.setdefault((rel, node.name), info)
            index.by_name.setdefault(node.name, []).append(info)
    return tree, text


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.C`` for a Name/Attribute chain, else None (subscripts, calls, …)."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        head = _dotted(expr.value)
        return None if head is None else f"{head}.{expr.attr}"
    return None


def _sdk_name(qualified: str) -> str | None:
    """The SDK harness base *qualified* names, if it is one imported from the SDK."""
    module, _, name = qualified.rpartition(".")
    if name not in _SDK_HARNESS_BASES:
        return None
    if module == _SDK_E2E_MODULE or module.startswith(f"{_SDK_E2E_MODULE}."):
        return name
    return None


def _resolve_base(
    expr: ast.expr, from_file: str, index: _Index
) -> tuple[set[str], list[_ClassInfo]]:
    """Resolve one base expression to ``(sdk bases, repo classes)``."""
    dotted = _dotted(expr)
    if dotted is None:
        return set(), []
    head, _, rest = dotted.partition(".")
    origins = index.origins.get(from_file, {})

    if not rest:
        same_file = index.classes.get((from_file, head))
        if same_file is not None:
            return set(), [same_file]

    origin = origins.get(head)
    if origin is not None:
        if rest and origin.startswith(f"{head}."):
            # ``import a.b.c`` binds ``a``: the chain as written is already
            # fully qualified.
            qualified = dotted
        else:
            qualified = f"{origin}.{rest}" if rest else origin
        sdk = _sdk_name(qualified)
        if sdk is not None:
            return {sdk}, []
        # Imported from somewhere other than the SDK: the referent is a repo
        # class (e.g. a generated base) if it is one, and otherwise nothing this
        # rule can grade — a same-named class from another package is not the
        # SDK's.
        return set(), list(index.by_name.get(qualified.rpartition(".")[2], []))

    # No import binding: a relative or star import, or a dotted chain off an
    # unbound name. Resolve the tail against the repo's own classes.
    return set(), list(index.by_name.get(dotted.rpartition(".")[2], []))


def _sdk_bases_reached(info: _ClassInfo, index: _Index) -> set[str]:
    """Every SDK harness base *info* transitively inherits."""
    reached: set[str] = set()
    seen: set[tuple[str, str]] = set()
    stack = [info]
    while stack:
        current = stack.pop()
        key = (current.file, current.node.name)
        if key in seen:
            continue
        seen.add(key)
        for base in current.node.bases:
            sdk, repo = _resolve_base(base, current.file, index)
            reached |= sdk
            stack.extend(repo)
    return reached


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _bases_text(node: ast.ClassDef) -> str:
    names = [ast.unparse(base) for base in node.bases]
    return ", ".join(names) if names else "no bases"


def _system_message(class_name: str, declared: str, bases: str, sdk: str) -> str:
    return (
        f"atlan.yaml declares type '{declared}', but e2e test class "
        f"{class_name!r} (bases: {bases}) resolves to {sdk}, not "
        f"{_SYSTEM_BASE}. A system app runs only inside a tenant, and a suite "
        f"that does not inherit {_SYSTEM_BASE} never opts into the system-app "
        "tenant pool, so it runs on the connector tenants instead. Migrate in "
        f"this order: make {_SYSTEM_BASE} (from application_sdk.testing.e2e) the "
        "first base — regenerating with `pkl eval -m . contract/app.pkl` does "
        "this for the generated base — and then get the repo added to the "
        "selected repositories of the E2E_SYSTEM_TENANT_MATRIX_JSON secret. "
        f"See {_DOCS_LINK}."
    )


def _connector_message(class_name: str, declared: str, bases: str) -> str:
    return (
        f"atlan.yaml declares type '{declared}', but e2e test class "
        f"{class_name!r} (bases: {bases}) inherits {_SYSTEM_BASE}. The "
        "system-app tenant pool is for system apps only; connectors test on the "
        "connector pool. Base the suite on the generated <Name>GeneratedE2EBase "
        "(or BaseE2ETest / SQLAppE2ETest). If this app genuinely is a system "
        "app, fix its declared type in contract/app.pkl and regenerate rather "
        f"than suppressing this finding. See {_DOCS_LINK}."
    )


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: T026 resolves inheritance across files; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Grade each SDK-harness e2e test class against ``atlan.yaml``'s ``type``."""
    declared = _declared_type(root)
    if declared is None:
        return []
    kind = declared.lower()
    if kind not in (_TYPE_SYSTEM, _TYPE_CONNECTOR):
        # utility (either base is allowed) and every other type: not graded.
        return []

    index = _Index(classes={}, by_name={}, origins={})
    for gen in _generated_base_files(root):
        _index_file(gen, gen.relative_to(root).as_posix(), index)

    e2e_dir = root / "tests" / "e2e"
    graded: list[tuple[str, str, ast.Module]] = []
    for path in paths:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        parsed = _index_file(path, rel, index)
        if (
            parsed is not None
            and path.is_relative_to(e2e_dir)
            and is_collectable_test_file(path.name)
        ):
            graded.append((rel, parsed[1], parsed[0]))

    findings: list[Finding] = []
    for rel, text, tree in graded:
        directives = _parse_directives(text)
        for node in ast.walk(tree):
            if not is_test_class(node):
                continue
            sdk = _sdk_bases_reached(_ClassInfo(rel, node), index)
            if not sdk:
                continue  # not an SDK-harness class
            is_system = _SYSTEM_BASE in sdk
            if kind == _TYPE_SYSTEM and not is_system:
                message = _system_message(
                    node.name, declared, _bases_text(node), " / ".join(sorted(sdk))
                )
            elif kind == _TYPE_CONNECTOR and is_system:
                message = _connector_message(node.name, declared, _bases_text(node))
            else:
                continue
            findings.append(
                make_finding(
                    filename=rel,
                    rule_id=RULE_T026,
                    node=node,
                    message=message,
                    directives=directives,
                )
            )
    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "T026: e2e harness base class must match the app's declared marketplace "
        "type (system -> SystemAppE2ETest; connector -> not SystemAppE2ETest)."
    ),
    default_scan_paths=("tests",),
)

if __name__ == "__main__":
    sys.exit(main())

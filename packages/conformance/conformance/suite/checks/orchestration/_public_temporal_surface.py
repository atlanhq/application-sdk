"""P007 RawTemporalInPublicSurface — no raw Temporal types in the public API.

The SDK must not hand callers raw ``temporalio`` objects through its public
surface, otherwise apps are coupled to the engine and the seam leaks (BLDX-1417).
Two leak shapes are flagged across the package:

* **Re-export leak** — a public package ``__init__`` lists a name in ``__all__``
  that was imported straight from ``temporalio``.  (The curated runtime-primitive
  re-exports in ``application_sdk/app/__init__.py`` are the sanctioned seam and are
  exempt.)
* **Signature leak** — a publicly re-exported function exposes a raw ``temporalio``
  type in a parameter or return annotation (e.g. ``create_temporal_client() ->
  Client``, ``create_worker(client: Client, …)``).

This is a cross-file rule: re-export ``__all__`` lives in an ``__init__`` while the
offending definition can live in an internal module, so it runs in ``scan_all``.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._temporal_common import (
    binding_node_for,
    collect_import_bindings,
    dunder_all_names,
    is_test_file,
    origin_is_temporal,
    signature_refs_temporal,
)

_BLESSED_PRIMITIVE_INIT = "application_sdk/app/__init__.py"


def _norm(rel: str) -> str:
    return rel.replace("\\", "/")


def _is_private_package(rel: str) -> bool:
    """True if any directory segment of *rel* is ``_``-prefixed (private package)."""
    parts = _norm(rel).split("/")[:-1]
    return any(p.startswith("_") for p in parts)


def _rel_to_module(rel: str) -> str:
    """Convert a repo-relative ``.py`` path to its dotted module path."""
    p = _norm(rel)
    for suffix in ("/__init__.py", ".py"):
        if p.endswith(suffix):
            p = p[: -len(suffix)]
            break
    return p.replace("/", ".").strip(".")


def _binding_module(origin: str | None) -> str | None:
    """Drop the trailing attribute from an import origin to get its module path.

    ``"application_sdk.execution._temporal.backend.create_temporal_client"`` ->
    ``"application_sdk.execution._temporal.backend"``.
    """
    if not origin:
        return None
    return origin.rsplit(".", 1)[0] if "." in origin else origin


def emit_p007(
    files: list[tuple[str, ast.Module]],
    directives_by_rel: dict[str, dict[int, _IgnoreDirective]],
) -> list[Finding]:
    """Emit P007 findings over the whole parsed file set.

    Pass A — walk every public package ``__init__`` and collect each publicly
    re-exported name together with the module(s) it could be defined in (the
    binding's origin module, plus the ``__init__``'s own module for names defined
    inline); flag any that re-export a raw ``temporalio`` symbol.

    Pass B — for a re-exported name, inspect only function definitions in its
    recorded origin module(s) and flag a signature that exposes a temporalio type.
    Matching on origin (not bare name) avoids flagging an unrelated function that
    merely shares a name with a re-exported symbol.

    Test files are skipped: they are never part of the public API surface, and the
    SDK's own tests legitimately exercise raw Temporal types.
    """
    findings: list[Finding] = []
    # name -> set of candidate defining modules (dotted).
    reexported_origins: dict[str, set[str]] = {}

    # Pass A — public re-export surface
    for rel, tree in files:
        if is_test_file(rel):
            continue
        if not _norm(rel).endswith("__init__.py") or _is_private_package(rel):
            continue
        all_names = dunder_all_names(tree)
        if not all_names:
            continue
        bindings = collect_import_bindings(tree)
        directives = directives_by_rel.get(rel, {})
        blessed = _norm(rel).endswith(_BLESSED_PRIMITIVE_INIT)
        init_module = _rel_to_module(rel)
        for name in all_names:
            origins = reexported_origins.setdefault(name, set())
            # The name may be defined inline in this __init__, or imported from
            # another module — record both as candidate defining modules.
            origins.add(init_module)
            origin_module = _binding_module(bindings.get(name))
            if origin_module:
                origins.add(origin_module)
            if blessed:
                continue
            if origin_is_temporal(bindings.get(name)):
                node = binding_node_for(tree, name) or tree
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id="P007",
                        node=node,
                        message=(
                            f"Public API re-exports a raw Temporal symbol '{name}' "
                            "from temporalio — this leaks the engine to callers. The "
                            "SDK must wrap it behind an opaque type or stop "
                            "re-exporting it. Suppress with "
                            "'# conformance: ignore[P007] <reason>'."
                        ),
                        directives=directives,
                    )
                )

    # Pass B — signatures of publicly re-exported definitions
    for rel, tree in files:
        if is_test_file(rel):
            continue
        module = _rel_to_module(rel)
        bindings = collect_import_bindings(tree)
        directives = directives_by_rel.get(rel, {})
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") or module not in reexported_origins.get(
                node.name, set()
            ):
                continue
            if signature_refs_temporal(node, bindings):
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id="P007",
                        node=node,
                        message=(
                            f"Public API '{node.name}' exposes a raw Temporal type "
                            "in its signature — callers are coupled to the engine. "
                            "Return/accept an opaque SDK type instead so the seam "
                            "stays closed. Suppress with "
                            "'# conformance: ignore[P007] <reason>'."
                        ),
                        directives=directives,
                    )
                )
    return findings

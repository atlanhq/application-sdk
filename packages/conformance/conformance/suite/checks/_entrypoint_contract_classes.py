"""Collect a per-entrypoint ``(wire_name, Input/Output contract class name)`` map.

Neither older collector is quite what the K-series needs:

* ``entrypoint_alignment._code_entrypoints`` (P016) derives wire names but not
  the entrypoint's contract classes.
* ``_entrypoint_contract_fields.collect_entrypoint_contract_names`` (shared,
  backs B005/B006) collects Input/Output class names but flattens them into a
  single ``frozenset`` with no association back to a wire name or entrypoint.

Two K-series rules need both together:

* **K006** resolves a manifest's ``$.extract.outputs.<field>`` reference to the
  *specific* entrypoint's Output class in a multi-entrypoint (bundle) app.
* **K016** resolves an entrypoint's boundary contracts — Input *and* Output —
  to the ``artifact_schemas.json`` that is supposed to describe their
  ``FileReference`` fields, which in a bundle lives under that entrypoint's own
  wire-named subdirectory.

It lives at ``suite/checks/`` top level rather than inside one rule's package
for the same reason ``_entrypoint_contract_fields`` does: it is a
contract-discovery primitive owned by no single rule series.

Wire-name derivation mirrors
``entrypoint_alignment._code_entrypoints._extract_ep_name`` exactly (bare
``@entrypoint`` -> kebab-cased method name; ``@entrypoint(name="literal")`` ->
the literal; non-literal ``name=`` -> unresolved, skipped here since P016
already reports it and neither rule can map an unresolved name to a manifest
subdir). It is duplicated locally rather than imported cross-series, matching
the existing precedent of small AST literal-extraction helpers living next to
their one caller.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from conformance.suite.checks.prescriptions._decorator_provenance import (
    ImportProvenance,
    is_entrypoint_decorator,
    is_task_decorator,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    resolve_ancestor,
)
from conformance.suite.checks.prescriptions._typed_boundaries import (
    _annotation_terminal_name,
    _get_non_self_params,
    _iter_class_body_methods,
)


def _base_name(base: ast.expr) -> str | None:
    """Return the simple name of a base-class expression (``Name`` or ``Attribute``)."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _method_name_to_kebab(name: str) -> str:
    """``'extract_metadata'`` -> ``'extract-metadata'`` (mirrors ``application_sdk.app.entrypoint``)."""
    return name.replace("_", "-")


def _extract_wire_name(deco: ast.expr, method_name: str) -> tuple[str | None, bool]:
    """Parse an ``@entrypoint`` decorator expression for its wire name.

    Returns ``(name, is_unresolved)``. Handles both ``ast.Name``/``ast.Attribute``
    bare-decorator forms (``@entrypoint`` / ``@app.entrypoint``) and their
    ``ast.Call`` counterparts (``@entrypoint(...)`` / ``@app.entrypoint(...)``) —
    unlike ``entrypoint_alignment._code_entrypoints._extract_ep_name`` (which this
    otherwise mirrors), the decorator-detection this module uses
    (``is_entrypoint_decorator``) also recognises attribute-form decorators via a
    module alias (e.g. ``import application_sdk.app as app`` -> ``@app.entrypoint``),
    so wire-name extraction must handle that form too or a matching entrypoint
    silently resolves to ``wire_name=None`` and never matches a manifest subdir.
    """
    if isinstance(deco, (ast.Name, ast.Attribute)):
        return _method_name_to_kebab(method_name), False
    if isinstance(deco, ast.Call) and isinstance(deco.func, (ast.Name, ast.Attribute)):
        for kw in deco.keywords:
            if kw.arg == "name":
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str
                ):
                    return kw.value.value, False
                return None, True
        return _method_name_to_kebab(method_name), False
    return None, False


@dataclass(frozen=True)
class EntrypointContract:
    """One entrypoint's wire name and (if resolvable) its boundary contract classes."""

    wire_name: str | None
    """Kebab-case wire name, or ``None`` for an implicit ``App.run()`` entrypoint
    (only meaningful for single-entrypoint-mode matching)."""

    output_class_name: str | None
    """De-aliased class name of the entrypoint's return-type annotation, or
    ``None`` when unannotated / not a simple class reference."""

    input_class_name: str | None = None
    """De-aliased class name of the entrypoint's first non-``self`` parameter
    annotation, or ``None`` when unannotated / not a simple class reference.

    Both directions are boundary surfaces: for a cross-app hand-off the
    *consumer* declares what it requires of its input, so an entrypoint's Input
    carries artifact declarations exactly like its Output does."""

    filename: str = ""
    """Repo-relative path of the file the entrypoint method is defined in."""


@dataclass
class CodeContractScan:
    """Accumulated per-entrypoint contract data across all scanned files."""

    entrypoints: list[EntrypointContract] = field(default_factory=list)


def scan_file_for_entrypoint_contracts(
    tree: ast.Module,
    filename: str,
    aliases: dict[str, str],
    prov: ImportProvenance,
    by_name: dict[str, ClassRecord],
    app_cache: dict[str, bool | None],
    result: CodeContractScan,
) -> None:
    """Scan one parsed module, appending discovered entrypoints to *result*.

    Mirrors the entrypoint-detection logic in
    ``_entrypoint_contract_fields.collect_entrypoint_contract_names`` (decorator
    provenance + implicit ``App.run()``), but keeps the wire name and Output
    class name paired per entrypoint instead of flattening into a name set.
    """
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue

        for func in _iter_class_body_methods(class_node):
            is_ep = False
            ep_deco: ast.expr | None = None

            for dec in func.decorator_list:
                if is_entrypoint_decorator(dec, prov):
                    is_ep = True
                    ep_deco = dec
                    break
            if not is_ep and any(
                is_task_decorator(dec, prov) for dec in func.decorator_list
            ):
                continue  # @task — skip entirely

            if (
                not is_ep
                and func.name == "run"
                and isinstance(func, ast.AsyncFunctionDef)
            ):
                for base in class_node.bases:
                    bname = _base_name(base)
                    if bname is None:
                        continue
                    bname = aliases.get(bname, bname)
                    if (
                        bname == "App"
                        or resolve_ancestor(bname, "App", by_name, app_cache, set())
                        is True
                    ):
                        is_ep = True
                        break

            if not is_ep:
                continue

            wire_name: str | None = None
            if ep_deco is not None:
                wire_name, is_unresolved = _extract_wire_name(ep_deco, func.name)
                if is_unresolved:
                    # P016 (K001-adjacent) already reports unresolved names;
                    # K006 cannot map an unresolved name to a manifest subdir.
                    continue
            # else: implicit App.run() — wire_name stays None (single-EP only).

            output_name: str | None = None
            if func.returns is not None:
                name = _annotation_terminal_name(func.returns)
                if name:
                    output_name = aliases.get(name, name)

            input_name: str | None = None
            non_self = _get_non_self_params(func)
            if non_self and non_self[0].annotation is not None:
                name = _annotation_terminal_name(non_self[0].annotation)
                if name:
                    input_name = aliases.get(name, name)

            result.entrypoints.append(
                EntrypointContract(
                    wire_name=wire_name,
                    output_class_name=output_name,
                    input_class_name=input_name,
                    filename=filename,
                )
            )

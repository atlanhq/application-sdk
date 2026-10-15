"""P059 JsonEncodedContractField — JSON text carried in a ``str`` contract field.

A ``str`` or ``list[str]`` field on an ``Input``/``Output`` contract whose value
the app writes with ``json.dumps`` (or ``model_dump_json``) or reads back with
``json.loads`` (or ``model_validate_json`` / ``TypeAdapter.validate_json``).
The contract says "text"; the data is a structure.  Payload safety (P001) and
the unmodeled-container check (P015) see only a string, so the shape and the
size of the value are hidden from both, and from the contract ledger.

Cross-file (``scan_all`` pass): the field is declared in the contracts module
and encoded or decoded in the task code, so evidence is matched by field name
across every scanned file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._contract_common import (
    _container_element_type,
    _terminal_name,
    collect_foreign_contract_names,
    is_str_annotation,
    iter_contract_classes,
    strip_annotation_wrappers,
)

_JSON_MODULES: frozenset[str] = frozenset({"json", "orjson", "ujson", "simplejson"})
_DECODE_METHODS: frozenset[str] = frozenset({"model_validate_json", "validate_json"})
_ENCODE_METHODS: frozenset[str] = frozenset({"model_dump_json"})
_LIST_LIKE_NAMES: frozenset[str] = frozenset(
    {
        "list",
        "List",
        "Sequence",
        "MutableSequence",
        "set",
        "Set",
        "frozenset",
        "FrozenSet",
    }
)


def _is_str_node(node: ast.expr) -> bool:
    return is_str_annotation(strip_annotation_wrappers(node))


def _is_text_field(annotation: ast.expr) -> bool:
    node = strip_annotation_wrappers(annotation)
    if _is_str_node(node):
        return True
    if not isinstance(node, ast.Subscript):
        return False
    if _terminal_name(node.value) not in _LIST_LIKE_NAMES:
        return False
    elem = _container_element_type(node)
    return elem is not None and _is_str_node(elem)


def _json_module_aliases(tree: ast.AST) -> frozenset[str]:
    aliases = set(_JSON_MODULES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _JSON_MODULES and alias.asname:
                    aliases.add(alias.asname)
    return frozenset(aliases)


def _json_function_aliases(tree: ast.AST, name: str) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _JSON_MODULES:
            for alias in node.names:
                if alias.name == name:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_json_call(
    node: ast.expr, name: str, bare: set[str], modules: frozenset[str]
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == name:
        return isinstance(func.value, ast.Name) and func.value.id in modules
    return isinstance(func, ast.Name) and func.id in bare


def _is_decode_call(node: ast.expr, loads: set[str], modules: frozenset[str]) -> bool:
    if _is_json_call(node, "loads", loads, modules):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DECODE_METHODS
    )


def _is_encode_value(node: ast.expr, dumps: set[str], modules: frozenset[str]) -> bool:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "decode"
    ):
        node = node.func.value
    if _is_json_call(node, "dumps", dumps, modules):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _ENCODE_METHODS
    )


def _encoded_field_value(
    node: ast.expr, dumps: set[str], modules: frozenset[str]
) -> bool:
    if _is_encode_value(node, dumps, modules):
        return True
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _is_encode_value(node.elt, dumps, modules)
    return False


def _field_attr(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _comprehension_fields(node: ast.AST) -> dict[str, str]:
    bound: dict[str, str] = {}
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for gen in node.generators:
            attr = _field_attr(gen.iter)
            if attr is not None and isinstance(gen.target, ast.Name):
                bound[gen.target.id] = attr
    return bound


def _collect_evidence(
    tree: ast.AST, contract_names: frozenset[str]
) -> dict[str, tuple[str, int]]:
    loads = _json_function_aliases(tree, "loads")
    dumps = _json_function_aliases(tree, "dumps")
    modules = _json_module_aliases(tree)
    evidence: dict[str, tuple[str, int]] = {}

    def note(field: str, kind: str, node: ast.AST) -> None:
        evidence.setdefault(field, (kind, getattr(node, "lineno", 0)))

    for node in ast.walk(tree):
        bound = _comprehension_fields(node)
        if bound:
            elt = getattr(node, "elt", None)
            if elt is not None and _is_decode_call(elt, loads, modules) and elt.args:
                arg = elt.args[0]
                if isinstance(arg, ast.Name) and arg.id in bound:
                    note(bound[arg.id], "decoded", elt)
        if isinstance(node, ast.Call):
            if _is_decode_call(node, loads, modules) and node.args:
                attr = _field_attr(node.args[0])
                if attr is not None:
                    note(attr, "decoded", node)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "map"
                and len(node.args) == 2
            ):
                fn, src = node.args
                attr = _field_attr(src)
                is_loads = (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "loads"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in modules
                ) or (isinstance(fn, ast.Name) and fn.id in loads)
                if attr is not None and is_loads:
                    note(attr, "decoded", node)
            builds_contract = _terminal_name(node.func) in contract_names
            for kw in node.keywords:
                if (
                    builds_contract
                    and kw.arg is not None
                    and _encoded_field_value(kw.value, dumps, modules)
                ):
                    note(kw.arg, "encoded", kw.value)
        if isinstance(node, ast.Assign) and _encoded_field_value(
            node.value, dumps, modules
        ):
            for target in node.targets:
                attr = _field_attr(target)
                if attr is not None:
                    note(attr, "encoded", node)
    return evidence


def check_p059(
    file_trees: dict[Path, ast.AST],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P059 for text contract fields whose value is encoded or decoded as JSON."""
    contract_names = frozenset(
        classdef.name
        for tree in file_trees.values()
        for classdef in iter_contract_classes(
            tree, collect_foreign_contract_names(tree)
        )
    )
    evidence: dict[str, tuple[Path, str, int]] = {}
    for path, tree in file_trees.items():
        for field, (kind, line) in _collect_evidence(tree, contract_names).items():
            evidence.setdefault(field, (path, kind, line))

    findings: list[Finding] = []
    for path, tree in file_trees.items():
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        foreign = collect_foreign_contract_names(tree)
        for classdef in iter_contract_classes(tree, foreign):
            for stmt in classdef.body:
                if not (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and _is_text_field(stmt.annotation)
                ):
                    continue
                name = stmt.target.id
                hit = evidence.get(name)
                if hit is None:
                    continue
                ev_path, kind, line = hit
                try:
                    ev_rel = str(ev_path.relative_to(root))
                except ValueError:
                    ev_rel = str(ev_path)
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id="P059",
                        node=stmt,
                        message=(
                            f"Contract field '{name}' on '{classdef.name}' carries JSON "
                            f"text: its value is {kind} as JSON at {ev_rel}:{line}. The "
                            "contract declares a string, so payload safety (P001), the "
                            "unmodeled-container check (P015) and the contract ledger "
                            "cannot see the real shape or its size. Declare the decoded "
                            "value as a typed field instead (a pydantic.BaseModel, or "
                            "list[Model] for a list of records); hand a source-sized "
                            "value over as a FileReference. Suppress with "
                            "'# conformance: ignore[P059] <reason>' only when a system "
                            "outside the app needs the value as JSON text."
                        ),
                        directives=file_directives.get(path, {}),
                    )
                )
    return findings

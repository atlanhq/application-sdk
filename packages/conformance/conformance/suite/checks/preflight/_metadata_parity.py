"""P035 PreflightMetadataContractParity.

On the gate path ``PreflightInput.metadata`` is rebuilt from the extraction
input's ``model_dump()``, so only fields declared on an entrypoint ``Input``
contract survive. A metadata key read inside ``preflight_check`` that is absent
from the union of every entrypoint Input contract's field names and aliases is
silently missing there, and a defensive ``.get(key, default)`` read passes
vacuously with the wrong config.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks._entrypoint_contract_fields import resolve_contract_fields
from conformance.suite.checks.prescriptions._error_code_prefix import ClassRecord
from conformance.suite.checks.prescriptions._typed_boundaries import (
    _get_non_self_params,
)
from conformance.suite.schema.findings import Finding

from ._common import (
    Registry,
    collect_entrypoint_input_contract_names,
    find_preflight_check_sites,
    norm_key,
)

_P035 = "P035"


def scan(reg: Registry) -> list[Finding]:
    input_contracts = collect_entrypoint_input_contract_names(reg)
    if not input_contracts:
        return []

    allowed: set[str] = set()
    for name in input_contracts:
        rec = reg.by_name.get(name)
        if rec is None:
            # Entrypoint input is an external/generated type we cannot resolve —
            # its field set is unknown, so we cannot make a parity claim.
            return []
        if _opts_into_extras(name, reg, set()):
            return []
        aliases = reg.aliases_by_rel.get(rec.file, {})
        for fi in resolve_contract_fields(rec.node, aliases, reg.by_name):
            allowed.add(norm_key(fi.name))
        allowed.update(norm_key(a) for a in _collect_field_aliases(name, reg, set()))

    findings: list[Finding] = []
    for src, func in find_preflight_check_sites(reg):
        params = _get_non_self_params(func)
        if not params:
            continue
        param = params[0].arg
        for key, node in _metadata_reads(func, param):
            if norm_key(key) not in allowed:
                findings.append(
                    make_finding(
                        filename=src.rel,
                        rule_id=_P035,
                        node=node,
                        message=(
                            f"preflight_check reads metadata key '{key}', which is not a "
                            f"field on any entrypoint Input contract "
                            f"({', '.join(sorted(input_contracts))}). On the gate path "
                            "metadata is rebuilt from the extraction input's model_dump, "
                            "so this key is silently absent and the read passes vacuously. "
                            "Declare it on the extraction input contract or stop reading it."
                        ),
                        directives=src.directives,
                    )
                )
    return findings


def _metadata_reads(
    func: ast.AsyncFunctionDef, param: str
) -> list[tuple[str, ast.AST]]:
    """Return ``(key, node)`` for literal ``<param>.metadata.get("k")`` / ``[...]`` reads."""
    reads: list[tuple[str, ast.AST]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _is_metadata_get(node, param):
            if node.args and _str_const(node.args[0]) is not None:
                reads.append((_str_const(node.args[0]), node))  # type: ignore[arg-type]
        elif isinstance(node, ast.Subscript) and _is_metadata_attr(node.value, param):
            key = _str_const(node.slice)
            if key is not None:
                reads.append((key, node))
    return reads


def _is_metadata_get(call: ast.Call, param: str) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "get"
        and _is_metadata_attr(func.value, param)
    )


def _is_metadata_attr(node: ast.expr, param: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "metadata"
        and isinstance(node.value, ast.Name)
        and node.value.id == param
    )


def _str_const(node: ast.expr) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _collect_field_aliases(name: str, reg: Registry, seen: set[str]) -> set[str]:
    """Field aliases declared on *name* and its in-repo ancestor contracts."""
    if name in seen:
        return set()
    seen.add(name)
    rec = reg.by_name.get(name)
    if rec is None:
        return set()
    aliases: set[str] = set()
    for stmt in rec.node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
            aliases.update(_field_call_aliases(stmt.value))
    for base in rec.bases:
        aliases.update(_collect_field_aliases(base, reg, seen))
    return aliases


def _field_call_aliases(call: ast.Call) -> set[str]:
    if not _is_field_call(call):
        return set()
    out: set[str] = set()
    for kw in call.keywords:
        if kw.arg in ("alias", "serialization_alias"):
            val = _str_const(kw.value)
            if val is not None:
                out.add(val)
        elif kw.arg == "validation_alias":
            if isinstance(kw.value, ast.Call):
                out.update(a for a in map(_str_const, kw.value.args) if a is not None)
            else:
                val = _str_const(kw.value)
                if val is not None:
                    out.add(val)
    return out


def _is_field_call(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "Field") or (
        isinstance(func, ast.Attribute) and func.attr == "Field"
    )


def _opts_into_extras(name: str, reg: Registry, seen: set[str]) -> bool:
    """True if *name* or an in-repo ancestor allows extra/unbounded fields."""
    if name in seen:
        return False
    seen.add(name)
    rec: ClassRecord | None = reg.by_name.get(name)
    if rec is None:
        return False
    for kw in rec.node.keywords:
        if (
            kw.arg == "allow_unbounded_fields"
            and isinstance(kw.value, ast.Constant)
            and bool(kw.value.value)
        ):
            return True
    for stmt in rec.node.body:
        targets = (
            stmt.targets
            if isinstance(stmt, ast.Assign)
            else [stmt.target]
            if isinstance(stmt, ast.AnnAssign)
            else []
        )
        is_model_config = any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in targets
        )
        if is_model_config and isinstance(stmt.value, ast.Call):
            for kw in stmt.value.keywords:
                if (
                    kw.arg == "extra"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "allow"
                ):
                    return True
    return any(_opts_into_extras(base, reg, seen) for base in rec.bases)

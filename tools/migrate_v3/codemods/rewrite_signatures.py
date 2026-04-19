"""A3: Rewrite workflow_args / workflow_config signatures to typed contracts.

Transforms method signatures of the form:

    async def fetch_databases(self, workflow_args: Dict[str, Any]) -> Optional[ActivityStatistics]:

to:

    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:

Detection criteria:
- Method name is present in contract_mapping, AND
- Has a param named ``workflow_args`` or ``workflow_config``

Already-migrated check (skip condition):
- Has a param named ``input`` with a type annotation ending in ``Input``

The optional *connector_type* kwarg disambiguates method names that appear in
multiple templates (e.g. ``run``).
"""

from __future__ import annotations

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod
from tools.migrate_v3.contract_mapping import resolve_method_contract

_OLD_PARAM_NAMES = frozenset({"workflow_args", "workflow_config"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_param_name(param: cst.Param) -> str:
    if isinstance(param.name, cst.Name):
        return param.name.value
    return ""


def _annotation_type_name(ann: cst.Annotation | None) -> str:
    """Return the top-level type name from an annotation (best-effort)."""
    if ann is None:
        return ""
    expr = ann.annotation
    if isinstance(expr, cst.Name):
        return expr.value
    if isinstance(expr, cst.Attribute) and isinstance(expr.attr, cst.Name):
        return expr.attr.value
    return ""


def _has_old_style_param(params: cst.Parameters) -> bool:
    return any(_get_param_name(p) in _OLD_PARAM_NAMES for p in params.params)


def _already_migrated(params: cst.Parameters) -> bool:
    """True if any param is named ``input`` and has a type ending in ``Input``."""
    for param in params.params:
        if _get_param_name(param) == "input":
            name = _annotation_type_name(param.annotation)
            if name.endswith("Input"):
                return True
    return False


# ---------------------------------------------------------------------------
# Codemod
# ---------------------------------------------------------------------------


class RewriteSignaturesCodemod(BaseCodemod):
    """A3: Replace workflow_args param and return type with typed contracts."""

    def __init__(self, connector_type: str | None = None) -> None:
        super().__init__()
        self._connector_type = connector_type

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        method_name = updated_node.name.value
        contract = resolve_method_contract(method_name, self._connector_type)
        if contract is None:
            return updated_node

        params = updated_node.params

        if _already_migrated(params):
            return updated_node

        if not _has_old_style_param(params):
            return updated_node

        # Build new parameter list: keep self, replace the old-style param.
        new_params_list: list[cst.Param] = []
        replaced = False
        for param in params.params:
            name = _get_param_name(param)
            if name in _OLD_PARAM_NAMES and not replaced:
                new_param = cst.Param(
                    name=cst.Name("input"),
                    annotation=cst.Annotation(
                        annotation=cst.Name(contract.input_type),
                    ),
                )
                new_params_list.append(new_param)
                self.changes.append(
                    f"Rewrote {method_name} param: {name} → input: {contract.input_type}"
                )
                replaced = True
            else:
                new_params_list.append(param)

        new_params = params.with_changes(params=new_params_list)
        new_returns = cst.Annotation(annotation=cst.Name(contract.output_type))

        self.changes.append(
            f"Updated {method_name} return type → {contract.output_type}"
        )
        self.imports_to_add.append((contract.import_module, contract.input_type))
        self.imports_to_add.append((contract.import_module, contract.output_type))

        return updated_node.with_changes(
            params=new_params,
            returns=new_returns,
        )

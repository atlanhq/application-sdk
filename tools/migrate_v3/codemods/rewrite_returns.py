"""A4: Rewrite ActivityStatistics / TaskStatistics return calls to typed Output constructors.

Transforms:

    return ActivityStatistics(total_record_count=N, ...)
    # or
    return TaskStatistics(total_record_count=N, ...)

to:

    return FetchDatabasesOutput(total_record_count=N, ...)

Also rewrites type annotations within method bodies:

    result: Optional[ActivityStatistics] = None
    →
    result: Optional[FetchDatabasesOutput] = None

The output type is resolved from contract_mapping using the enclosing method
name tracked via a visit/leave stack.
"""

from __future__ import annotations

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod
from tools.migrate_v3.contract_mapping import resolve_method_contract

_OLD_RETURN_NAMES = frozenset({"ActivityStatistics", "TaskStatistics"})


class RewriteReturnsCodemod(BaseCodemod):
    """A4: Replace ActivityStatistics(...)/TaskStatistics(...) → XxxOutput(...)."""

    def __init__(self, connector_type: str | None = None) -> None:
        super().__init__()
        self._connector_type = connector_type
        # Stack of method names — supports nested functions.
        self._method_stack: list[str] = []

    # ------------------------------------------------------------------
    # Method name tracking
    # ------------------------------------------------------------------

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._method_stack.append(node.name.value)
        return True

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        if self._method_stack:
            self._method_stack.pop()
        return updated_node

    # ------------------------------------------------------------------
    # Call-site rewrite: ActivityStatistics(...) → XxxOutput(...)
    # ------------------------------------------------------------------

    def leave_Call(
        self,
        original_node: cst.Call,
        updated_node: cst.Call,
    ) -> cst.Call:
        if not self._method_stack:
            return updated_node

        func = updated_node.func
        if not (isinstance(func, cst.Name) and func.value in _OLD_RETURN_NAMES):
            return updated_node

        method_name = self._method_stack[-1]
        contract = resolve_method_contract(method_name, self._connector_type)
        if contract is None:
            return updated_node

        old_name = func.value
        self.changes.append(
            f"Replaced {old_name}() with {contract.output_type}() in {method_name}"
        )
        self.imports_to_add.append((contract.import_module, contract.output_type))
        return updated_node.with_changes(
            func=func.with_changes(value=contract.output_type)
        )

    # ------------------------------------------------------------------
    # Annotation rewrite: Optional[ActivityStatistics] → Optional[XxxOutput]
    # ------------------------------------------------------------------

    def leave_Annotation(
        self,
        original_node: cst.Annotation,
        updated_node: cst.Annotation,
    ) -> cst.Annotation:
        if not self._method_stack:
            return updated_node

        method_name = self._method_stack[-1]
        contract = resolve_method_contract(method_name, self._connector_type)
        if contract is None:
            return updated_node

        ann = updated_node.annotation

        # Simple: ActivityStatistics → XxxOutput
        if isinstance(ann, cst.Name) and ann.value in _OLD_RETURN_NAMES:
            old_name = ann.value
            self.changes.append(
                f"Updated annotation: {old_name} → {contract.output_type} in {method_name}"
            )
            self.imports_to_add.append((contract.import_module, contract.output_type))
            return updated_node.with_changes(
                annotation=ann.with_changes(value=contract.output_type)
            )

        # Wrapped: Optional[ActivityStatistics] → Optional[XxxOutput]
        if (
            isinstance(ann, cst.Subscript)
            and isinstance(ann.value, cst.Name)
            and ann.value.value == "Optional"
            and ann.slice
        ):
            first = ann.slice[0]
            if (
                isinstance(first, cst.SubscriptElement)
                and isinstance(first.slice, cst.Index)
                and isinstance(first.slice.value, cst.Name)
                and first.slice.value.value in _OLD_RETURN_NAMES
            ):
                old_name = first.slice.value.value
                new_slice = first.with_changes(
                    slice=first.slice.with_changes(
                        value=first.slice.value.with_changes(value=contract.output_type)
                    )
                )
                new_ann = ann.with_changes(slice=[new_slice])
                self.changes.append(
                    f"Updated annotation: Optional[{old_name}] → Optional[{contract.output_type}]"
                    f" in {method_name}"
                )
                self.imports_to_add.append(
                    (contract.import_module, contract.output_type)
                )
                return updated_node.with_changes(annotation=new_ann)

        return updated_node

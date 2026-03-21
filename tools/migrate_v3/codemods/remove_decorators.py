"""A1: Remove v2 Temporal decorators and replace @activity.defn with @task.

Transforms:
- Class decorators:  @workflow.defn              → (removed)
- Method decorators: @workflow.run               → (removed, no @task added)
                     @activity.defn              → @task(timeout_seconds=N)
                     @activity.defn(name="...")  → @task(timeout_seconds=N)
                     @auto_heartbeater           → (removed)

Timeout N is looked up from contract_mapping by method name.  Unknown
methods default to 600 s.  Unrelated decorators (@staticmethod, etc.) are
preserved unchanged.
"""

from __future__ import annotations

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod
from tools.migrate_v3.contract_mapping import resolve_method_contract

_DEFAULT_TASK_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Decorator-pattern helpers
# ---------------------------------------------------------------------------


def _attr_matches(expr: cst.BaseExpression, obj: str, attr: str) -> bool:
    """True if *expr* is the dotted name ``obj.attr``."""
    return (
        isinstance(expr, cst.Attribute)
        and isinstance(expr.value, cst.Name)
        and expr.value.value == obj
        and isinstance(expr.attr, cst.Name)
        and expr.attr.value == attr
    )


def _is_workflow_defn(dec: cst.Decorator) -> bool:
    expr = dec.decorator
    if _attr_matches(expr, "workflow", "defn"):
        return True
    if isinstance(expr, cst.Call) and _attr_matches(expr.func, "workflow", "defn"):
        return True
    return False


def _is_workflow_run(dec: cst.Decorator) -> bool:
    return _attr_matches(dec.decorator, "workflow", "run")


def _is_activity_defn(dec: cst.Decorator) -> bool:
    expr = dec.decorator
    if _attr_matches(expr, "activity", "defn"):
        return True
    if isinstance(expr, cst.Call) and _attr_matches(expr.func, "activity", "defn"):
        return True
    return False


def _is_auto_heartbeater(dec: cst.Decorator) -> bool:
    expr = dec.decorator
    if isinstance(expr, cst.Name) and expr.value == "auto_heartbeater":
        return True
    if (
        isinstance(expr, cst.Call)
        and isinstance(expr.func, cst.Name)
        and expr.func.value == "auto_heartbeater"
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Codemod
# ---------------------------------------------------------------------------


class RemoveDecoratorsCodemod(BaseCodemod):
    """A1: Strip v2 Temporal decorators; insert @task where appropriate."""

    def __init__(self, connector_type: str | None = None) -> None:
        super().__init__()
        self._connector_type = connector_type

    def _get_timeout(self, method_name: str) -> int:
        contract = resolve_method_contract(method_name, self._connector_type)
        if contract is not None and contract.timeout_seconds is not None:
            return contract.timeout_seconds
        return _DEFAULT_TASK_TIMEOUT

    # ------------------------------------------------------------------
    # Class-level: remove @workflow.defn
    # ------------------------------------------------------------------

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        new_decs: list[cst.Decorator] = []
        class_name = updated_node.name.value
        changed = False

        for dec in updated_node.decorators:
            if _is_workflow_defn(dec):
                self.changes.append(f"Removed @workflow.defn from class {class_name}")
                changed = True
            else:
                new_decs.append(dec)

        if not changed:
            return updated_node
        return updated_node.with_changes(decorators=new_decs)

    # ------------------------------------------------------------------
    # Method-level: remove @workflow.run / @activity.defn / @auto_heartbeater
    # ------------------------------------------------------------------

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        method_name = updated_node.name.value
        new_decs: list[cst.Decorator] = []
        removed_activity_defn = False
        changed = False

        for dec in updated_node.decorators:
            if _is_activity_defn(dec):
                removed_activity_defn = True
                self.changes.append(f"Removed @activity.defn from {method_name}")
                changed = True
            elif _is_workflow_run(dec):
                self.changes.append(f"Removed @workflow.run from {method_name}")
                changed = True
            elif _is_auto_heartbeater(dec):
                self.changes.append(f"Removed @auto_heartbeater from {method_name}")
                changed = True
            else:
                new_decs.append(dec)

        if removed_activity_defn:
            timeout = self._get_timeout(method_name)
            task_dec = cst.Decorator(
                decorator=cst.parse_expression(f"task(timeout_seconds={timeout})"),
            )
            new_decs = [task_dec] + new_decs
            self.changes.append(
                f"Added @task(timeout_seconds={timeout}) to {method_name}"
            )
            if ("application_sdk.app", "task") not in self.imports_to_add:
                self.imports_to_add.append(("application_sdk.app", "task"))

        if not changed:
            return updated_node
        return updated_node.with_changes(decorators=new_decs)

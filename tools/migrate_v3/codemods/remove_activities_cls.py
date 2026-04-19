"""A6: Remove v2 activities plumbing (activities_cls, get_activities, activities_instance).

Removes three categories of v2-only boilerplate that have no equivalent in v3:

1. Class-level ``activities_cls`` attribute (plain or annotated):
       activities_cls = MyActivities
       activities_cls: Type[MyActivities] = MyActivities

2. ``get_activities()`` static method used for Temporal worker registration:
       @staticmethod
       def get_activities(activities: MyActivities) -> list:
           return [activities.fetch_databases, ...]

3. ``activities_instance = self.activities_cls()`` assignment inside method bodies:
       activities_instance = self.activities_cls()
       activities_instance: MyActivities = self.activities_cls()

Only exact name matches are removed — connector-specific helpers like
``get_asset_extraction_activities()`` are NOT touched.
"""

from __future__ import annotations

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_staticmethod(func: cst.FunctionDef) -> bool:
    for dec in func.decorators:
        if (
            isinstance(dec.decorator, cst.Name)
            and dec.decorator.value == "staticmethod"
        ):
            return True
    return False


def _is_activities_cls_stmt(stmt: cst.BaseSmallStatement) -> bool:
    """True if *stmt* assigns to the name ``activities_cls``."""
    if isinstance(stmt, cst.Assign):
        return any(
            isinstance(t.target, cst.Name) and t.target.value == "activities_cls"
            for t in stmt.targets
        )
    if isinstance(stmt, cst.AnnAssign):
        return (
            isinstance(stmt.target, cst.Name) and stmt.target.value == "activities_cls"
        )
    return False


def _is_self_activities_cls_call(expr: cst.BaseExpression) -> bool:
    """True if *expr* is ``self.activities_cls()``."""
    return (
        isinstance(expr, cst.Call)
        and isinstance(expr.func, cst.Attribute)
        and isinstance(expr.func.value, cst.Name)
        and expr.func.value.value == "self"
        and isinstance(expr.func.attr, cst.Name)
        and expr.func.attr.value == "activities_cls"
    )


def _is_activities_instance_stmt(stmt: cst.BaseSmallStatement) -> bool:
    """True if *stmt* is ``activities_instance = self.activities_cls()``."""
    if isinstance(stmt, cst.Assign):
        if not any(
            isinstance(t.target, cst.Name) and t.target.value == "activities_instance"
            for t in stmt.targets
        ):
            return False
        return _is_self_activities_cls_call(stmt.value)
    if isinstance(stmt, cst.AnnAssign):
        if not (
            isinstance(stmt.target, cst.Name)
            and stmt.target.value == "activities_instance"
        ):
            return False
        return stmt.value is not None and _is_self_activities_cls_call(stmt.value)
    return False


# ---------------------------------------------------------------------------
# Codemod
# ---------------------------------------------------------------------------


class RemoveActivitiesClsCodemod(BaseCodemod):
    """A6: Strip activities_cls attribute, get_activities() method, and activities_instance line."""

    def __init__(self) -> None:
        super().__init__()
        self._class_depth: int = 0
        self._function_depth: int = 0

    # ------------------------------------------------------------------
    # Depth tracking
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._class_depth += 1
        return True

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        self._class_depth -= 1
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._function_depth += 1
        return True

    # ------------------------------------------------------------------
    # get_activities() static method removal (class body, any depth)
    # ------------------------------------------------------------------

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef | cst.RemovalSentinel:
        # Decrement AFTER checking so depth is still "inside" when deciding.
        try:
            if (
                self._class_depth > 0
                and updated_node.name.value == "get_activities"
                and _has_staticmethod(updated_node)
            ):
                self.changes.append("Removed get_activities() static method")
                return cst.RemovalSentinel.REMOVE
            return updated_node
        finally:
            self._function_depth -= 1

    # ------------------------------------------------------------------
    # activities_cls attribute + activities_instance line removal
    # ------------------------------------------------------------------

    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
        for stmt in updated_node.body:
            # Class-level: activities_cls = ... (not inside any method)
            if self._class_depth > 0 and self._function_depth == 0:
                if _is_activities_cls_stmt(stmt):
                    self.changes.append("Removed activities_cls class attribute")
                    return cst.RemovalSentinel.REMOVE

            # Method body: activities_instance = self.activities_cls()
            if self._function_depth > 0:
                if _is_activities_instance_stmt(stmt):
                    self.changes.append(
                        "Removed activities_instance = self.activities_cls() line"
                    )
                    return cst.RemovalSentinel.REMOVE

        return updated_node

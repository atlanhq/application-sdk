"""A2: Rewrite ``workflow.execute_activity_method(...)`` calls to ``await self.method(arg)``.

Transforms:

    # Instance-method form (most common)
    result = await workflow.execute_activity_method(
        activities_instance.fetch_databases,
        args=[workflow_args],
        retry_policy=...,
        start_to_close_timeout=...,
        heartbeat_timeout=...,
        summary=...,
    )

    # Class-method form (positional arg)
    result = await workflow.execute_activity_method(
        MyActivities.get_workflow_args,
        workflow_config,
        retry_policy=...,
    )

To:

    result = await self.fetch_databases(workflow_args)
    result = await self.get_workflow_args(workflow_config)

Dynamic dispatch (``getattr``-based) is NOT rewritten — a change entry is recorded
so the caller knows to handle it manually.
"""

from __future__ import annotations

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod

# Temporal kwargs that have no equivalent in the v3 @task model.
_KWARGS_TO_STRIP = frozenset(
    {
        "retry_policy",
        "start_to_close_timeout",
        "heartbeat_timeout",
        "summary",
        "schedule_to_close_timeout",
        "task_queue",
        "cancellation_type",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_execute_activity_method(func: cst.BaseExpression) -> bool:
    """True if *func* is ``workflow.execute_activity_method``."""
    return (
        isinstance(func, cst.Attribute)
        and isinstance(func.value, cst.Name)
        and func.value.value == "workflow"
        and isinstance(func.attr, cst.Name)
        and func.attr.value == "execute_activity_method"
    )


def _extract_method_name(first_arg: cst.Arg) -> str | None:
    """Return the method name from the first arg, or None for dynamic dispatch."""
    val = first_arg.value
    # Both ``activities_instance.method`` and ``ClassName.method`` are Attribute nodes.
    if isinstance(val, cst.Attribute) and isinstance(val.attr, cst.Name):
        return val.attr.value
    # Name (variable) or Call (getattr result) → dynamic dispatch
    return None


def _extract_data_arg(call: cst.Call) -> cst.BaseExpression | None:
    """Find the data argument passed to the activity.

    Looks for:
    1. ``args=[x]`` keyword argument → returns ``x``
    2. Second positional argument → returns it directly
    """
    positional: list[cst.Arg] = []
    for arg in call.args:
        kw = arg.keyword
        if kw is not None and isinstance(kw, cst.Name) and kw.value == "args":
            # args=[x] or args=(x,) — take the first element
            val = arg.value
            if isinstance(val, cst.List) and val.elements:
                return val.elements[0].value
            if isinstance(val, cst.Tuple) and val.elements:
                return val.elements[0].value
            return None
        if kw is None:
            positional.append(arg)

    # Second positional arg (index 1) is the data arg; index 0 is the method ref.
    if len(positional) >= 2:
        return positional[1].value
    return None


# ---------------------------------------------------------------------------
# Codemod
# ---------------------------------------------------------------------------


class RewriteActivityCallsCodemod(BaseCodemod):
    """A2: Replace ``workflow.execute_activity_method(...)`` with ``await self.method(arg)``."""

    def leave_Call(
        self,
        original_node: cst.Call,
        updated_node: cst.Call,
    ) -> cst.BaseExpression:
        if not _is_execute_activity_method(updated_node.func):
            return updated_node

        if not updated_node.args:
            return updated_node

        first_arg = updated_node.args[0]
        method_name = _extract_method_name(first_arg)

        if method_name is None:
            # Dynamic dispatch — cannot auto-rewrite.
            self.changes.append(
                "SKIPPED: dynamic dispatch in execute_activity_method (rewrite manually)"
            )
            return updated_node

        data_arg = _extract_data_arg(updated_node)

        self.changes.append(
            f"Rewrote execute_activity_method({method_name}) → await self.{method_name}(...)"
        )

        # Build the new call from scratch via parse_expression to avoid
        # carrying over multiline whitespace from the original call structure.
        if data_arg is not None:
            arg_code = cst.Module(
                body=[cst.SimpleStatementLine(body=[cst.Expr(value=data_arg)])]
            ).code.strip()
            return cst.parse_expression(f"self.{method_name}({arg_code})")
        return cst.parse_expression(f"self.{method_name}()")

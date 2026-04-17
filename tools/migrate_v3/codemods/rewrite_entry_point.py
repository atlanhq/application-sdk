"""A7: Rewrite ``BaseApplication`` entry point to ``run_dev_combined``.

Detects the standard v2 ``main()`` pattern:

    async def main():
        application = BaseApplication(name=APP_NAME, client_class=..., handler_class=...)
        await application.setup_workflow([(WorkflowClass, ActivitiesClass)])
        await application.start(workflow_class=WorkflowClass, has_configmap=True)

And replaces the body with:

    async def main():
        await run_dev_combined(AppNameApp, handler_class=AppNameHandler)

The App class name is derived from the connector's root directory using the
``atlan-{app-name}-app`` naming convention (e.g. ``atlan-anaplan-app`` → ``AnaplanApp``).
Pass ``app_class_name`` explicitly to override.

Complex entry points (split worker/server, ``include_router``, conditional logic) are
detected and left unchanged — a TODO comment is prepended to the function body instead.
"""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod

_TODO_COMMENT = "# TODO(upgrade-v3): custom entry point — rewrite manually"

# Temporal-specific kwargs to BaseApplication.start() that have no v3 equivalent.
_COMPLEX_METHOD_NAMES = frozenset(
    {"start_worker", "start_server", "setup_server", "include_router"}
)


# ---------------------------------------------------------------------------
# Public helper: derive App class name from directory
# ---------------------------------------------------------------------------


def derive_app_class_name(root: Path) -> str:
    """Return the PascalCase App class name derived from a connector directory.

    Follows the ``atlan-{app-name}-app`` naming convention:
    - ``atlan-anaplan-app`` → ``AnaplanApp``
    - ``atlan-schema-registry-app`` → ``SchemaRegistryApp``

    Falls back to ``"MyApp"`` if the directory name does not match the pattern.
    """
    name = root.name
    if name.startswith("atlan-"):
        name = name[6:]
    if name.endswith("-app"):
        name = name[:-4]
    if not name:
        return "MyApp"
    parts = name.split("-")
    return "".join(p.capitalize() for p in parts) + "App"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_base_application_call(expr: cst.BaseExpression) -> bool:
    return (
        isinstance(expr, cst.Call)
        and isinstance(expr.func, cst.Name)
        and expr.func.value == "BaseApplication"
    )


def _find_base_application(
    stmts: list[cst.BaseStatement],
) -> tuple[str, cst.Call] | None:
    """Return (variable_name, call_node) if a ``BaseApplication(...)`` assignment exists."""
    for stmt in stmts:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for s in stmt.body:
            if (
                isinstance(s, cst.Assign)
                and isinstance(s.value, cst.Call)
                and _is_base_application_call(s.value)
            ):
                if s.targets and isinstance(s.targets[0].target, cst.Name):
                    return s.targets[0].target.value, s.value
            if (
                isinstance(s, cst.AnnAssign)
                and isinstance(s.value, cst.Call)
                and _is_base_application_call(s.value)
            ):
                if isinstance(s.target, cst.Name):
                    return s.target.value, s.value
    return None


def _extract_kwarg_name(call: cst.Call, kwarg_name: str) -> str | None:
    """Return the Name value of a specific keyword argument from a Call."""
    for arg in call.args:
        if (
            arg.keyword is not None
            and isinstance(arg.keyword, cst.Name)
            and arg.keyword.value == kwarg_name
            and isinstance(arg.value, cst.Name)
        ):
            return arg.value.value
    return None


def _has_complex_indicators(stmts: list[cst.BaseStatement]) -> bool:
    """True if the function body contains patterns that block auto-migration."""
    for stmt in stmts:
        # Any control-flow statement indicates conditional entry-point logic.
        if isinstance(stmt, (cst.If, cst.For, cst.While, cst.Try)):
            return True
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for s in stmt.body:
            # Check for await calls to complex methods.
            expr = None
            if isinstance(s, cst.Expr) and isinstance(s.value, cst.Await):
                expr = s.value.expression
            elif isinstance(s, cst.Assign) and isinstance(s.value, cst.Await):
                expr = s.value.expression
            if expr is None:
                continue
            if isinstance(expr, cst.Call) and isinstance(expr.func, cst.Attribute):
                attr = expr.func.attr
                if isinstance(attr, cst.Name) and attr.value in _COMPLEX_METHOD_NAMES:
                    return True
    return False


def _prepend_todo(body: cst.IndentedBlock) -> cst.IndentedBlock:
    """Prepend a TODO comment to the first statement of *body*."""
    stmts = list(body.body)
    if not stmts:
        return body
    first = stmts[0]
    comment_line = cst.EmptyLine(
        indent=True,
        comment=cst.Comment(_TODO_COMMENT),
    )
    # SimpleStatementLine and compound statements both have leading_lines.
    if isinstance(first, (cst.SimpleStatementLine, cst.BaseCompoundStatement)):
        existing = list(first.leading_lines)  # type: ignore[attr-defined]
        updated = first.with_changes(leading_lines=[comment_line] + existing)
        return body.with_changes(body=[updated] + stmts[1:])
    return body


# ---------------------------------------------------------------------------
# Codemod
# ---------------------------------------------------------------------------


class RewriteEntryPointCodemod(BaseCodemod):
    """A7: Replace BaseApplication entry point with run_dev_combined."""

    def __init__(self, app_class_name: str | None = None) -> None:
        super().__init__()
        self._app_class_name = app_class_name or "MyApp"

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        if updated_node.name.value != "main":
            return updated_node

        body = updated_node.body
        if not isinstance(body, cst.IndentedBlock):
            return updated_node

        stmts = list(body.body)

        found = _find_base_application(stmts)
        if found is None:
            return updated_node  # No BaseApplication in this main() — skip.

        _var_name, base_app_call = found

        if _has_complex_indicators(stmts):
            self.changes.append(
                "Skipped complex entry point (TODO comment inserted — manual rewrite required)"
            )
            return updated_node.with_changes(body=_prepend_todo(body))

        # Extract handler_class if present.
        handler_class = _extract_kwarg_name(base_app_call, "handler_class")

        # Build: await run_dev_combined(AppName, handler_class=Handler)
        new_args: list[cst.Arg] = [cst.Arg(value=cst.Name(self._app_class_name))]
        if handler_class:
            new_args.append(
                cst.Arg(
                    keyword=cst.Name("handler_class"),
                    value=cst.Name(handler_class),
                    equal=cst.AssignEqual(
                        whitespace_before=cst.SimpleWhitespace(""),
                        whitespace_after=cst.SimpleWhitespace(""),
                    ),
                )
            )

        new_stmt = cst.SimpleStatementLine(
            body=[
                cst.Expr(
                    value=cst.Await(
                        expression=cst.Call(
                            func=cst.Name("run_dev_combined"),
                            args=new_args,
                        ),
                        whitespace_after_await=cst.SimpleWhitespace(" "),
                    )
                )
            ]
        )
        new_body = body.with_changes(body=[new_stmt])

        desc = f"run_dev_combined({self._app_class_name}"
        if handler_class:
            desc += f", handler_class={handler_class}"
        desc += ")"
        self.changes.append(f"Rewrote BaseApplication entry point → {desc}")

        self.imports_to_add.append(("application_sdk.main", "run_dev_combined"))
        self.imports_to_remove.append(
            ("application_sdk.application", "BaseApplication")
        )

        return updated_node.with_changes(body=new_body)

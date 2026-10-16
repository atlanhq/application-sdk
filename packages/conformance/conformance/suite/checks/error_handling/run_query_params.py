"""E022 — a SQL client's ``run_query`` override drops the SDK base's ``params``."""

from __future__ import annotations

import ast

from ._sql_client import RUN_QUERY, RUN_QUERY_PARAM


def _run_query_override(
    node: ast.ClassDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The ``run_query`` the class body defines, if it defines one.

    The last definition wins, as it does when Python executes the body.
    """
    for stmt in reversed(node.body):
        if (
            isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
            and stmt.name == RUN_QUERY
        ):
            return stmt
    return None


def _accepts_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if a call passing ``params=`` reaches *func* without a TypeError.

    A named ``params`` (positional or keyword-only) or a ``**kwargs`` catch-all
    both accept it; ``*args`` alone does not, because callers pass it by name.
    """
    args = func.args
    if args.kwarg is not None:
        return True
    return any(
        a.arg == RUN_QUERY_PARAM
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    )


class RunQueryParamsMixin:
    """Rule method for E022 (sql-bind-parameters)."""

    # ── E022 ─────────────────────────────────────────────────────────────────

    def _check_e022(self, node: ast.ClassDef) -> None:
        if node.name not in self._sql_client_classes:
            return
        override = _run_query_override(node)
        if override is None or _accepts_params(override):
            return
        self._add(
            "E022",
            override,
            f"{node.name}.run_query overrides BaseSQLClient.run_query without its "
            f"`params` argument, so a caller that binds values "
            f"(`run_query(sql, params={{...}})`) gets a TypeError instead of a "
            f"bound query. Add `params: Mapping[str, SqlBindValue] | None = None` "
            f"and bind it to the query, raising SqlBindParameterError for a name "
            f"the query never references; never run the query without it.",
        )

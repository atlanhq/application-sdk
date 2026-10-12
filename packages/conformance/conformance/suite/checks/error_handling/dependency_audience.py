"""E021 — a DEPENDENCY_UNAVAILABLE error with its audience overridden to USER."""

from __future__ import annotations

import ast


def _assigns_user_audience(node: ast.ClassDef) -> ast.stmt | None:
    """The ``audience = Audience.USER`` statement in *node*'s body, if any."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            value: ast.expr | None = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
        else:
            continue
        if (
            any(isinstance(t, ast.Name) and t.id == "audience" for t in targets)
            and isinstance(value, ast.Attribute)
            and value.attr == "USER"
        ):
            return stmt
    return None


class DependencyAudienceMixin:
    """Rule method for E021 (failure-category)."""

    # ── E021 ─────────────────────────────────────────────────────────────────

    def _check_e021(self, node: ast.ClassDef) -> None:
        if node.name not in self._dependency_classes:
            return
        override = _assigns_user_audience(node)
        if override is None:
            return
        self._add(
            "E021",
            override,
            f"{node.name} is a DEPENDENCY_UNAVAILABLE error with audience "
            f"overridden to USER, i.e. a customer-owned system is down. That is "
            f"SOURCE_UNAVAILABLE: re-base it on SourceUnavailableError, rename "
            f"its code to SOURCE_UNAVAILABLE_*, and drop the override. "
            f"DEPENDENCY_UNAVAILABLE never blocks a hard preflight gate, so the "
            f"run starts and fails later on the same fault.",
        )

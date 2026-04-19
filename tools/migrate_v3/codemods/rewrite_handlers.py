"""A5: Rewrite handler *args/**kwargs → typed contracts, delete load().

Transforms (inside classes inheriting from a handler base):

    async def test_auth(self, *args: Any, **kwargs: Any) -> bool:
        ...
    →
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        ...

Same for ``preflight_check`` (PreflightInput/Output) and
``fetch_metadata`` (MetadataInput/Output).

Also deletes the ``load()`` method entirely if present.

Only applies inside classes whose base list includes one of:
    Handler, BaseHandler, DefaultHandler, BaseSQLHandler, HandlerInterface
"""

from __future__ import annotations

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod
from tools.migrate_v3.contract_mapping import resolve_method_contract

_HANDLER_BASES = frozenset(
    {"Handler", "BaseHandler", "DefaultHandler", "BaseSQLHandler", "HandlerInterface"}
)
_HANDLER_METHODS = frozenset({"test_auth", "preflight_check", "fetch_metadata"})


def _extract_base_name(base: cst.Arg) -> str:
    """Extract the simple class name from a base class argument."""
    val = base.value
    if isinstance(val, cst.Name):
        return val.value
    if isinstance(val, cst.Attribute) and isinstance(val.attr, cst.Name):
        return val.attr.value
    return ""


class RewriteHandlersCodemod(BaseCodemod):
    """A5: Typed handler contracts + delete load()."""

    def __init__(self) -> None:
        super().__init__()
        # Stack — True when we're inside a handler class, supports nesting.
        self._handler_class_stack: list[bool] = []

    # ------------------------------------------------------------------
    # Class scope tracking
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        is_handler = any(
            _extract_base_name(base) in _HANDLER_BASES for base in node.bases
        )
        self._handler_class_stack.append(is_handler)
        return True

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        if self._handler_class_stack:
            self._handler_class_stack.pop()
        return updated_node

    @property
    def _in_handler_class(self) -> bool:
        return bool(self._handler_class_stack) and self._handler_class_stack[-1]

    # ------------------------------------------------------------------
    # Method transforms
    # ------------------------------------------------------------------

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef | cst.RemovalSentinel:
        if not self._in_handler_class:
            return updated_node

        method_name = updated_node.name.value

        # Delete load() entirely.
        if method_name == "load":
            self.changes.append("Deleted load() method from handler class")
            return cst.RemovalSentinel.REMOVE

        # Only rewrite the three typed handler methods.
        if method_name not in _HANDLER_METHODS:
            return updated_node

        contract = resolve_method_contract(method_name, "handler")
        if contract is None:
            return updated_node

        # Skip if already typed (has 'input' param with annotation ending in 'Input').
        for param in updated_node.params.params:
            if (
                isinstance(param.name, cst.Name)
                and param.name.value == "input"
                and param.annotation is not None
            ):
                ann = param.annotation.annotation
                if isinstance(ann, cst.Name) and ann.value.endswith("Input"):
                    return updated_node

        # Rebuild params: (self, input: XxxInput).
        self_param = next(
            (
                p
                for p in updated_node.params.params
                if isinstance(p.name, cst.Name) and p.name.value == "self"
            ),
            None,
        )
        new_input_param = cst.Param(
            name=cst.Name("input"),
            annotation=cst.Annotation(annotation=cst.Name(contract.input_type)),
        )
        new_params_list = (
            [self_param, new_input_param]
            if self_param is not None
            else [new_input_param]
        )
        new_params = cst.Parameters(params=new_params_list)
        new_returns = cst.Annotation(annotation=cst.Name(contract.output_type))

        self.changes.append(
            f"Rewrote {method_name} → (self, input: {contract.input_type})"
            f" -> {contract.output_type}"
        )
        self.imports_to_add.append((contract.import_module, contract.input_type))
        self.imports_to_add.append((contract.import_module, contract.output_type))

        return updated_node.with_changes(
            params=new_params,
            returns=new_returns,
        )

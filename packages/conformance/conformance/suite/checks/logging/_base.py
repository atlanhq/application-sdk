"""Shared abstract state for L-series rule mixins.

All category mixins inherit from ``_MixinBase`` so pyright sees exactly one
declaration of each shared attribute (``_framework``, ``_is_adapter_file``,
etc.) rather than multiple incompatible overrides across mixin classes.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective
from conformance.suite.schema.findings import Finding

from ._helpers import Framework


class _MixinBase:
    """Declares the attributes injected by ``Checker.__init__``.

    Mixins inherit this class; ``Checker`` provides the concrete values.
    """

    _filename: str
    _directives: dict[int, _IgnoreDirective]
    _framework: Framework
    _is_adapter_file: bool
    _findings: list[Finding]
    _loop_stack: list[ast.For | ast.AsyncFor | ast.While]
    _except_stack: list[ast.ExceptHandler]
    _in_main_block: int

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None: ...

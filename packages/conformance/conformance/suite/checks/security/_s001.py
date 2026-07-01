"""S001 HardcodedCredential — flag string literals stored as credential values.

A non-empty string literal assigned to (or passed as) a credential-named target
is a hardcoded secret.  Detection is deliberately conservative — empty strings,
``Field(default=…)`` calls (the value is a ``Call``, not a literal), format/URL
templates, SCREAMING_SNAKE env-var-name *references*, self-referential field-name
strings, and ``Enum`` members are all excluded — because the surveyed fleet has
zero production violations, so this rule is a future-drift guard that must not add
noise.
"""

from __future__ import annotations

import ast
import re

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._secret_names import is_credential_value_name

# A value that is itself an env-var *name* (SCREAMING_SNAKE) is a reference, not
# the secret — e.g. ``client_secret = "ATLAN_OAUTH2_CLIENT_SECRET"``.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Bases that mark a class as an enumeration; assignments in an enum body are
# member declarations, not credential storage.
_ENUM_BASES: frozenset[str] = frozenset(
    {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
)

_MESSAGE = (
    "Hardcoded credential: string literal stored as '{name}'. Resolve the secret "
    "at runtime via the SDK secret store (context.resolve_credential / a "
    "CredentialRef, or the SecretStore protocol) instead of embedding it in source. "
    "Suppress a reviewed exception with '# conformance: ignore[S001] <reason>'."
)


def _is_flaggable_secret_literal(value: ast.expr, target_name: str) -> bool:
    """True if *value* is a string literal that looks like a real secret value."""
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        return False
    text = value.value
    if not text:  # empty-string default / placeholder
        return False
    if "{" in text:  # format / URL template (e.g. "...{password}...")
        return False
    if _ENV_NAME_RE.match(text):  # an env-var-NAME reference, not the secret
        return False
    if text.lower() == target_name.lower():  # self-referential field/enum label
        return False
    return True


class HardcodedCredentialChecker(ast.NodeVisitor):
    """Walk a module AST and emit S001 findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._findings: list[Finding] = []
        self._enum_depth = 0

    def _add(self, node: ast.AST, name: str) -> None:
        self._findings.append(
            make_finding(
                filename=self._filename,
                rule_id="S001",
                node=node,
                message=_MESSAGE.format(name=name),
                directives=self._directives,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        is_enum = any(
            (isinstance(b, ast.Name) and b.id in _ENUM_BASES)
            or (isinstance(b, ast.Attribute) and b.attr in _ENUM_BASES)
            for b in node.bases
        )
        if is_enum:
            self._enum_depth += 1
        self.generic_visit(node)
        if is_enum:
            self._enum_depth -= 1

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._enum_depth == 0:
            for target in node.targets:
                if isinstance(target, ast.Name) and is_credential_value_name(target.id):
                    if _is_flaggable_secret_literal(node.value, target.id):
                        self._add(node.value, target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (
            self._enum_depth == 0
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and is_credential_value_name(node.target.id)
            and _is_flaggable_secret_literal(node.value, node.target.id)
        ):
            self._add(node.value, node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if (
                kw.arg
                and is_credential_value_name(kw.arg)
                and _is_flaggable_secret_literal(kw.value, kw.arg)
            ):
                self._add(kw.value, kw.arg)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and is_credential_value_name(key.value)
                and _is_flaggable_secret_literal(value, key.value)
            ):
                self._add(value, key.value)
        self.generic_visit(node)

"""S001 HardcodedCredential — flag string literals stored as credential values.

A non-empty string literal assigned to (or passed as) a credential-named target
is a hardcoded secret.  Detection is deliberately conservative — empty strings,
``Field(default=…)`` calls (the value is a ``Call``, not a literal), format/URL
templates, SCREAMING_SNAKE env-var-name *references*, self-referential field-name
strings, message tables (a dict of SCREAMING_SNAKE code keys whose every value is a
help-text sentence with no token-shaped word), field-name alias maps (every value a known provider credential
field name), and ``Enum`` members are all excluded — because the surveyed fleet has
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

_KNOWN_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_account_id",
    }
)
_AUTH_SCHEME_RE = re.compile(r"^(?:bearer|basic|token|digest)\s", re.IGNORECASE)
_SENTENCE_END = (".", "!", "?")
_MIN_SENTENCE_WORDS = 6
_MIN_STOPWORDS = 2
_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with", "your"}
)
_TOKEN_PREFIXES = (
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "sk_",
    "sk-",
    "pk_",
    "rk_",
    "xox",
    "akia",
    "asia",
    "eyj",
    "bearer",
    "basic",
)
_MIN_RANDOM_WORD = 16

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


def _looks_like_secret_word(word: str) -> bool:
    lower = word.lower()
    parts = re.split(r"[^a-z0-9_-]+", lower)
    return any(part.startswith(_TOKEN_PREFIXES) for part in parts if part) or (
        len(word) >= _MIN_RANDOM_WORD
        and any(c.isdigit() for c in word)
        and any(c.isalpha() for c in word)
    )


def _looks_like_prose(text: str) -> bool:
    """True for a help sentence; never for a PEM block, auth value or embedded token."""
    stripped = text.strip()
    if "-----BEGIN" in stripped or _AUTH_SCHEME_RE.match(stripped):
        return False
    words = [w.strip(".,;:!?()'\"") for w in stripped.split()]
    return (
        len(words) >= _MIN_SENTENCE_WORDS
        and stripped.endswith(_SENTENCE_END)
        and sum(w.lower() in _STOPWORDS for w in words) >= _MIN_STOPWORDS
        and not any(_looks_like_secret_word(w) for w in words)
    )


def _str_value(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_field_alias_map(values: list[ast.expr]) -> bool:
    """True when every value is a known provider credential field name."""
    return bool(values) and all(_str_value(v) in _KNOWN_FIELD_NAMES for v in values)


def _is_message_table(node: ast.Dict) -> bool:
    """True for a dict of SCREAMING_SNAKE code keys whose every value is a sentence."""
    keys = [_str_value(k) for k in node.keys]
    values = [_str_value(v) for v in node.values]
    return (
        len(values) >= 2
        and all(k is not None and _ENV_NAME_RE.match(k) for k in keys)
        and all(v is not None and _looks_like_prose(v) for v in values)
    )


def _target_credential_name(target: ast.expr) -> str | None:
    """Return the credential-name to check for a ``Name``/``Attribute``/``Subscript``
    target.

    Covers ``password = "..."``, ``self.password = "..."``, and
    ``cfg["password"] = "..."`` alike — all are hardcoded-credential surfaces (the
    attribute form is common in ``__init__`` methods and settings classes; the
    subscript form in config dicts built via item assignment rather than a
    dict-literal).
    """
    if isinstance(target, ast.Name) and is_credential_value_name(target.id):
        return target.id
    if isinstance(target, ast.Attribute) and is_credential_value_name(target.attr):
        return target.attr
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and isinstance(target.slice.value, str)
        and is_credential_value_name(target.slice.value)
    ):
        return target.slice.value
    return None


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
                name = _target_credential_name(target)
                if name is not None and _is_flaggable_secret_literal(node.value, name):
                    self._add(node.value, name)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._enum_depth == 0 and node.value is not None:
            name = _target_credential_name(node.target)
            if name is not None and _is_flaggable_secret_literal(node.value, name):
                self._add(node.value, name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and _is_field_alias_map([kw.value for kw in node.keywords])
        ):
            self.generic_visit(node)
            return
        for kw in node.keywords:
            if (
                kw.arg
                and is_credential_value_name(kw.arg)
                and _is_flaggable_secret_literal(kw.value, kw.arg)
            ):
                self._add(kw.value, kw.arg)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        if _is_field_alias_map(node.values) or _is_message_table(node):
            self.generic_visit(node)
            return
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and is_credential_value_name(key.value)
                and _is_flaggable_secret_literal(value, key.value)
            ):
                self._add(value, key.value)
        self.generic_visit(node)

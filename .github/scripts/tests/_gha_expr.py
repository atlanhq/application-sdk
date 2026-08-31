"""A GitHub Actions expression evaluator, for testing workflow `if:` gates.

Job gates are conditional logic. `docs/standards/ci.md` says conditional logic
must be testable, and for a *shell* branch the answer is "move it into a Python
script". A job-level `if:` cannot move — GitHub evaluates it before any step
runs — so the only way to regression-test one is to evaluate the real expression,
lifted verbatim out of the workflow YAML, against synthetic event payloads.

That is what this module is for. It is deliberately NOT a general GHA
implementation: it covers the operators and functions the repo's gates actually
use, and raises :class:`UnsupportedExpression` on anything else. Refusing loudly
is the whole safety property — a partial evaluator that silently treated an
unknown construct as null would let a broken gate pass its own test, which is
worse than having no test.

Modelled on the documented semantics in
https://docs.github.com/actions/reference/workflows-and-actions/expressions —
the fiddly parts being that `&&`/`||` return an *operand* rather than a boolean,
that `==` on two strings is case-insensitive, and that mismatched types are cast
to numbers (with NaN comparing false against everything, itself included).
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "UnknownContext",
    "UnsupportedExpression",
    "evaluate",
    "evaluate_operand",
    "truthy",
]


class UnsupportedExpression(Exception):
    """The expression uses syntax or a function this evaluator does not model."""


class UnknownContext(Exception):
    """The expression reads a context root the caller did not supply.

    Raised rather than resolved to null so a test cannot pass vacuously: if a
    gate starts consulting ``needs`` or ``vars``, every scenario must say what
    that context holds instead of silently inheriting "absent, therefore false".
    """


# ── Lexer ────────────────────────────────────────────────────────────────────

# Order matters: two-character operators must be tried before their prefixes,
# or `!=` lexes as `!` followed by a stray `=`.
_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<string>'(?:[^']|'')*')
  | (?P<number>0[xX][0-9a-fA-F]+|[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>!])
  | (?P<punct>[().,\[\]*])
    # Context keys may contain '-' (`inputs.enable-e2e`). Safe because GitHub
    # expressions have no arithmetic operators, so '-' is never infix.
  | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*)
    """,
    re.VERBOSE,
)


def _lex(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(source):
        match = _TOKEN_RE.match(source, position)
        if match is None:
            raise UnsupportedExpression(
                f"cannot lex {source[position:position + 20]!r} at offset {position}"
            )
        position = match.end()
        kind = match.lastgroup
        assert kind is not None
        if kind == "space":
            continue
        tokens.append((kind, match.group()))
    return tokens


# ── Parser (recursive descent, GHA precedence) ───────────────────────────────
#
# Lowest to highest: || < && < ==/!= < </<=/>/>= < ! < postfix (.x, .*, [i]).
# Nodes are plain tuples so the evaluator below is a flat dispatch.

_Node = tuple[Any, ...]


class _Parser:
    def __init__(self, tokens: Sequence[tuple[str, str]], source: str) -> None:
        self._tokens = tokens
        self._source = source
        self._index = 0

    # -- token helpers --

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise UnsupportedExpression(
                f"unexpected end of expression: {self._source!r}"
            )
        self._index += 1
        return token

    def _accept(self, value: str) -> bool:
        token = self._peek()
        if token is not None and token[1] == value:
            self._index += 1
            return True
        return False

    def _expect(self, value: str) -> None:
        if not self._accept(value):
            raise UnsupportedExpression(
                f"expected {value!r} at token {self._index} of {self._source!r}"
            )

    # -- grammar --

    def parse(self) -> _Node:
        node = self._or()
        if self._peek() is not None:
            raise UnsupportedExpression(
                f"trailing tokens after a complete expression in {self._source!r}"
            )
        return node

    def _or(self) -> _Node:
        node = self._and()
        while self._accept("||"):
            node = ("or", node, self._and())
        return node

    def _and(self) -> _Node:
        node = self._equality()
        while self._accept("&&"):
            node = ("and", node, self._equality())
        return node

    def _equality(self) -> _Node:
        node = self._comparison()
        while True:
            token = self._peek()
            if token is None or token[1] not in ("==", "!="):
                return node
            self._index += 1
            node = (token[1], node, self._comparison())

    def _comparison(self) -> _Node:
        node = self._unary()
        while True:
            token = self._peek()
            if token is None or token[1] not in ("<", "<=", ">", ">="):
                return node
            self._index += 1
            node = (token[1], node, self._unary())

    def _unary(self) -> _Node:
        if self._accept("!"):
            return ("not", self._unary())
        return self._postfix()

    def _postfix(self) -> _Node:
        node = self._primary()
        while True:
            if self._accept("."):
                kind, text = self._take()
                if text == "*":
                    node = ("star", node)
                elif kind == "ident":
                    node = ("prop", node, text)
                else:
                    raise UnsupportedExpression(
                        f"expected a property name after '.' in {self._source!r}"
                    )
            elif self._accept("["):
                index = self._or()
                self._expect("]")
                node = ("index", node, index)
            else:
                return node

    def _primary(self) -> _Node:
        kind, text = self._take()
        if text == "(":
            node = self._or()
            self._expect(")")
            return node
        if kind == "string":
            # GHA escapes a single quote by doubling it.
            return ("lit", text[1:-1].replace("''", "'"))
        if kind == "number":
            if text.lower().startswith("0x"):
                return ("lit", float(int(text, 16)))
            return ("lit", float(text))
        if kind == "ident":
            lowered = text.lower()
            if lowered == "true":
                return ("lit", True)
            if lowered == "false":
                return ("lit", False)
            if lowered == "null":
                return ("lit", None)
            if self._accept("("):
                arguments: list[_Node] = []
                if not self._accept(")"):
                    arguments.append(self._or())
                    while self._accept(","):
                        arguments.append(self._or())
                    self._expect(")")
                return ("call", text, tuple(arguments))
            return ("context", text)
        raise UnsupportedExpression(f"unexpected token {text!r} in {self._source!r}")


# ── Value semantics ──────────────────────────────────────────────────────────

_NAN = float("nan")


def truthy(value: Any) -> bool:
    """GHA's cast-to-boolean: only null, false, 0, NaN and '' are falsy.

    Note what is *not* falsy: an empty array, an empty object, and the strings
    ``'0'`` and ``'false'``. That last one is the classic workflow footgun — a
    `workflow_dispatch` input arrives as a string, so a bare `if: inputs.flag`
    is true even when the flag reads "false".
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return True


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0.0
        try:
            if text.lower().startswith(("0x", "-0x", "+0x")):
                return float(int(text, 16))
            return float(text)
        except ValueError:
            return _NAN
    return _NAN


def _loose_eq(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        # Documented: string comparison is case-insensitive.
        return left.casefold() == right.casefold()
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        # Objects and arrays compare by reference, never by content.
        return left is right
    a, b = _number(left), _number(right)
    if math.isnan(a) or math.isnan(b):
        return False
    return a == b


def _property(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    if isinstance(value, list):
        # `labels.*.name` — a property read across a filtered array collects the
        # property from each element, dropping those that don't have it.
        return [e[name] for e in value if isinstance(e, dict) and name in e]
    return None


def _star(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return None


def _contains(haystack: Any, needle: Any) -> bool:
    if isinstance(haystack, str):
        return str(_stringify(needle)).casefold() in haystack.casefold()
    if isinstance(haystack, list):
        return any(_loose_eq(item, needle) for item in haystack)
    return False


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _starts_with(text: Any, prefix: Any) -> bool:
    return _stringify(text).casefold().startswith(_stringify(prefix).casefold())


def _ends_with(text: Any, suffix: Any) -> bool:
    return _stringify(text).casefold().endswith(_stringify(suffix).casefold())


def _join(values: Any, separator: Any = ",") -> str:
    items: Iterable[Any] = values if isinstance(values, list) else [values]
    return _stringify(separator).join(_stringify(v) for v in items)


#: Functions this evaluator models. `success()` / `failure()` / `cancelled()` are
#: deliberately absent: their value depends on runtime job state that a static
#: payload cannot express, so a gate using one must be tested another way rather
#: than against a guessed default.
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "contains": _contains,
    "startswith": _starts_with,
    "endswith": _ends_with,
    "join": _join,
    "always": lambda: True,
    # `cancelled()` is modelled at False, and it is the ONLY status function
    # that is. It appears in gates as `!cancelled() && <real condition>`, where
    # its job is purely to keep a dependent running when an upstream `needs`
    # job was skipped — it carries none of the gate's actual meaning, so
    # pinning it lets a test exercise the part that does.
    #
    # `success()` and `failure()` stay unmodelled on purpose: they DO carry
    # meaning, and guessing a value for them is how a gate scenario passes
    # vacuously. See test_always_is_modelled_but_success_is_not.
    "cancelled": lambda: False,
    "format": lambda template, *args: re.sub(
        r"\{(\d+)\}", lambda m: _stringify(args[int(m.group(1))]), _stringify(template)
    ),
}


# ── Evaluation ───────────────────────────────────────────────────────────────


def _eval(node: _Node, contexts: dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "context":
        root = node[1]
        if root not in contexts:
            raise UnknownContext(
                f"the expression reads the {root!r} context, which this scenario "
                f"does not supply (supplied: {sorted(contexts)}). Add it to the "
                "payload rather than letting it resolve to null."
            )
        return contexts[root]
    if kind == "prop":
        return _property(_eval(node[1], contexts), node[2])
    if kind == "star":
        return _star(_eval(node[1], contexts))
    if kind == "index":
        target = _eval(node[1], contexts)
        key = _eval(node[2], contexts)
        if isinstance(target, list):
            position = _number(key)
            if math.isnan(position) or position != int(position):
                return None
            position = int(position)
            return target[position] if 0 <= position < len(target) else None
        return _property(target, _stringify(key))
    if kind == "not":
        return not truthy(_eval(node[1], contexts))
    if kind == "and":
        left = _eval(node[1], contexts)
        # `&&` yields an operand, not a boolean — `'' && 'x'` is ''.
        return left if not truthy(left) else _eval(node[2], contexts)
    if kind == "or":
        left = _eval(node[1], contexts)
        return left if truthy(left) else _eval(node[2], contexts)
    if kind == "==":
        return _loose_eq(_eval(node[1], contexts), _eval(node[2], contexts))
    if kind == "!=":
        return not _loose_eq(_eval(node[1], contexts), _eval(node[2], contexts))
    if kind in ("<", "<=", ">", ">="):
        a = _number(_eval(node[1], contexts))
        b = _number(_eval(node[2], contexts))
        if math.isnan(a) or math.isnan(b):
            return False
        return {
            "<": a < b,
            "<=": a <= b,
            ">": a > b,
            ">=": a >= b,
        }[kind]
    if kind == "call":
        name = node[1].casefold()
        function = _FUNCTIONS.get(name)
        if function is None:
            raise UnsupportedExpression(
                f"function {node[1]}() is not modelled by this evaluator; add it to "
                "_FUNCTIONS with its documented semantics, or test that gate another way"
            )
        arguments = [_eval(argument, contexts) for argument in node[2]]
        return function(*arguments)
    raise UnsupportedExpression(f"unhandled node {kind!r}")


def _parse(expression: str) -> _Node:
    source = expression.strip()
    if source.startswith("${{") and source.endswith("}}"):
        source = source[3:-2].strip()
    if "${{" in source:
        raise UnsupportedExpression(
            f"partially-interpolated expressions are not supported: {expression!r}"
        )
    return _Parser(_lex(source), source).parse()


def evaluate(expression: str, contexts: dict[str, Any]) -> bool:
    """Evaluate a workflow `if:` expression and return whether the job runs.

    ``expression`` is the raw YAML value, with or without the surrounding
    ``${{ }}`` GitHub allows in an `if:`. ``contexts`` maps each context root the
    expression reads (``github``, ``inputs``, ``needs``, …) to its value; a root
    the expression reads but the caller omits raises :class:`UnknownContext`.
    """
    return truthy(_eval(_parse(expression), contexts))


def evaluate_operand(expression: str, contexts: dict[str, Any]) -> Any:
    """Evaluate like :func:`evaluate` but return the operand, not its truthiness.

    ``&&``/``||`` return an operand rather than a boolean in GHA, so this is the
    way to pin that semantics directly — ``evaluate`` alone would mask a broken
    evaluator that coerced the result to a boolean.
    """
    return _eval(_parse(expression), contexts)

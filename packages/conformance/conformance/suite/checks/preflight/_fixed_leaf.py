"""F021 PreflightFixedLeafInBroadExcept (CONNECT-1358).

Flags a broad ``except`` inside ``preflight_check`` (or a helper it reaches)
that builds an Auth- or Permission-rooted error on a path that no test on the
caught exception selects. Every failure in the ``try`` then reaches the
customer as a credential or grant problem: an empty credential, a DNS failure
and a 500 all read as "grant access". The SDK's own subclasses outside
``application_sdk.errors`` (``SqlClientAuthFailedError``, ``CredentialError``,
...) count too; a test pins ``_SDK_CUSTOMER_LEAVES`` to the SDK source.

Each leaf gets one of three outcomes from the conditions on its path (the
enclosing ``if`` / ``while`` / conditional-expression arm or ``match`` case,
and every earlier early-exit guard such as ``if C: raise`` in the same block):

* **selected** — reaching it requires a discriminating exception predicate or
  its complement (``isinstance(exc, PermissionError)``, a comparison on
  ``exc.status`` / ``type(exc)`` / ``str(exc)``, or the ``else`` of one);
* **unconditional** — it is reachable whatever was caught. Only this outcome
  is reported;
* **unknown** — the analysis cannot tell. No finding: a clean F021 is not a
  proof that the classification is right.

Supported: literals, ``not`` / ``and`` / ``or`` / conditional expressions,
direct type and data predicates on the exception, tautologies every caught
exception passes (``exc``, ``exc is not None``, ``isinstance(exc, Exception)``),
conditions that do not involve the exception (``self.strict``: may be true or
false, so it restricts an ``and`` but cannot excuse an ``or``), and simple
``name = expr`` aliases in straight-line code before the test. Everything else
touching the exception is unknown: any call it is passed to (a predicate or
classifier whose semantics F021 does not know, including a leaf handed to it
as a default), chained or unpacked targets, augmented assignment, and names
bound off the leaf's straight-line path (in an earlier compound statement, a
nested ``except``, a loop, or a walrus). An attribute store on an alias
(``err.detail = ...``) does not rebind it. A nested ``except`` is judged on
its own. Known limits: an early exit only counts as a flat ``if C: raise`` /
``return`` in the same block (not one nested in ``with`` or both arms of an
inner ``if``, nor an ``assert``), and comprehension filters are ignored.
"""

from __future__ import annotations

import ast
import enum
from collections.abc import Iterator

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, iter_function_nodes, reachable_preflight_sites
from ._contracts import _Checker, _qualified

_F021 = "F021"

_BROAD = frozenset({"Exception", "BaseException"})
_CUSTOMER_ROOTS = frozenset({"AuthError", "AppPermissionDeniedError"})
_SDK_CUSTOMER_LEAVES = frozenset(
    {
        "AwsAssumeRoleError",
        "AwsRdsTokenError",
        "AzureClientAuthError",
        "AzureCredentialError",
        "CredentialError",
        "CredentialNotFoundError",
        "CredentialParseError",
        "CredentialValidationError",
        "OAuthTokenError",
        "SqlAwsCredentialsError",
        "SqlClientAuthFailedError",
        "StoragePermissionError",
    }
)
_DATA_CALLS = frozenset({"type", "str", "repr", "getattr"})
_BLOCK_FIELDS = frozenset({"body", "orelse", "finalbody", "cases"})
_EXITS = (ast.Raise, ast.Return, ast.Continue, ast.Break)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_NEW_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_OWN_SCOPE_ENDS = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ExceptHandler,
)


class _Reach(enum.Enum):
    """Whether one condition on a path lets an arbitrary exception through."""

    NEVER = "never"
    REQUIRES = "requires an exception predicate"
    UNKNOWN = "unknown"
    FREE = "free"


class _Outcome(enum.Enum):
    SELECTED = "selected"
    UNCONDITIONAL = "unconditional"
    UNKNOWN = "unknown"


class _Kind(enum.Enum):
    """How a local name relates to the caught exception."""

    EXCEPTION = "the exception object"
    DATA = "data read from the exception"
    BOOL = "a known guard value"
    UNKNOWN = "unknown"


_Value = tuple[_Kind, _Reach, _Reach]
_Env = dict[str, _Value]
_Parents = dict[ast.AST, tuple[ast.AST, str, int]]

_UNKNOWN_VALUE: _Value = (_Kind.UNKNOWN, _Reach.UNKNOWN, _Reach.UNKNOWN)
_UNRELATED = frozenset({_Reach.FREE, _Reach.NEVER})


def _all(*reaches: _Reach) -> _Reach:
    """Every condition must hold on this path."""
    for strongest in (_Reach.NEVER, _Reach.REQUIRES, _Reach.UNKNOWN):
        if strongest in reaches:
            return strongest
    return _Reach.FREE


def _any(*reaches: _Reach) -> _Reach:
    """At least one of these paths is taken."""
    for weakest in (_Reach.FREE, _Reach.UNKNOWN, _Reach.REQUIRES):
        if weakest in reaches:
            return weakest
    return _Reach.NEVER


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _OWN_SCOPE_ENDS):
            continue
        yield child
        yield from _own_nodes(child)


def _is_broad(src: Source, handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_qualified(src, kind).rsplit(".", 1)[-1] in _BROAD for kind in kinds)


def _customer_rooted(
    checker: _Checker, src: Source, node: ast.AST, seen: frozenset[int] = frozenset()
) -> bool:
    name = _qualified(src, node)
    if name.startswith("application_sdk."):
        return name.rsplit(".", 1)[-1] in _CUSTOMER_ROOTS | _SDK_CUSTOMER_LEAVES
    resolved = checker.symbol(src, node)
    if resolved is None:
        return False
    owner, cls = resolved
    if not isinstance(cls, ast.ClassDef) or id(cls) in seen:
        return False
    return any(
        _customer_rooted(checker, owner, base, seen | {id(cls)}) for base in cls.bases
    )


def _kind(node: ast.expr, env: _Env) -> _Kind | None:
    """How ``node``'s value relates to the exception; ``None`` when it does not."""
    if isinstance(node, ast.Name):
        value = env.get(node.id)
        if value is None:
            return None
        kind, when_true, when_false = value
        if kind is _Kind.BOOL and {when_true, when_false} <= _UNRELATED:
            return None
        return kind
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        base = _kind(node.value, env)
        if base in (_Kind.EXCEPTION, _Kind.DATA):
            return _Kind.DATA
        return None if base is None else _Kind.UNKNOWN
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
        if node.func.id in _DATA_CALLS:
            if _kind(node.args[0], env) in (_Kind.EXCEPTION, _Kind.DATA):
                return _Kind.DATA
    related = any(
        isinstance(n, ast.Name) and _kind(n, env) is not None for n in ast.walk(node)
    )
    return _Kind.UNKNOWN if related else None


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _reach(node: ast.expr, want: bool, env: _Env) -> _Reach:
    """Whether ``node`` can evaluate to ``want`` for an arbitrary exception."""
    if isinstance(node, ast.Constant):
        return _Reach.FREE if bool(node.value) == want else _Reach.NEVER
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _reach(node.operand, not want, env)
    if isinstance(node, ast.BoolOp):
        parts = [_reach(value, want, env) for value in node.values]
        every = isinstance(node.op, ast.And) == want
        return _all(*parts) if every else _any(*parts)
    if isinstance(node, ast.IfExp):
        return _any(
            _all(_reach(node.test, True, env), _reach(node.body, want, env)),
            _all(_reach(node.test, False, env), _reach(node.orelse, want, env)),
        )
    if isinstance(node, ast.Call) and _is_isinstance(node):
        subject = _kind(node.args[0], env)
        if subject is _Kind.EXCEPTION and _broad_types(node.args[1]):
            return _Reach.FREE if want else _Reach.NEVER
        if subject in (_Kind.EXCEPTION, _Kind.DATA):
            return _Reach.REQUIRES
    if isinstance(node, ast.Compare):
        return _compare(node, want, env)
    if isinstance(node, ast.Name) and node.id in env:
        kind, when_true, when_false = env[node.id]
        if kind is _Kind.BOOL:
            return when_true if want else when_false
        if kind is _Kind.EXCEPTION:
            return _Reach.FREE if want else _Reach.NEVER
        return _Reach.UNKNOWN
    return _Reach.UNKNOWN if _kind(node, env) else _Reach.FREE


def _is_isinstance(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
        and not node.keywords
    )


def _broad_types(kinds: ast.expr) -> bool:
    types = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
    return any(ast.unparse(t).rsplit(".", 1)[-1] in _BROAD for t in types)


def _compare(node: ast.Compare, want: bool, env: _Env) -> _Reach:
    operands = [node.left, *node.comparators]
    kinds = [_kind(operand, env) for operand in operands]
    if _Kind.UNKNOWN in kinds or _Kind.BOOL in kinds:
        return _Reach.UNKNOWN
    if not any(kinds):
        return _Reach.FREE
    if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
        left, right = operands
        if (kinds[0] is _Kind.EXCEPTION and _is_none(right)) or (
            kinds[1] is _Kind.EXCEPTION and _is_none(left)
        ):
            is_not_none = isinstance(node.ops[0], (ast.IsNot, ast.NotEq))
            return _Reach.FREE if is_not_none == want else _Reach.NEVER
    return _Reach.REQUIRES


def _is_boolean(node: ast.expr, env: _Env) -> bool:
    """Whether ``node`` only ever produces a truth value the evaluator models."""
    if isinstance(node, (ast.Constant, ast.Compare)):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    if isinstance(node, ast.Call):
        return _is_isinstance(node)
    if isinstance(node, ast.BoolOp):
        return all(_is_condition(value, env) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _is_condition(node.body, env) and _is_condition(node.orelse, env)
    if isinstance(node, ast.Name):
        value = env.get(node.id)
        return value is not None and value[0] is _Kind.BOOL
    return False


def _is_condition(node: ast.expr, env: _Env) -> bool:
    """A truth value, or a value that does not involve the exception at all."""
    return _is_boolean(node, env) or _kind(node, env) is None


def _value_of(node: ast.expr, env: _Env) -> _Value | None:
    """The value a simple ``name = node`` store binds; ``None`` when unrelated."""
    kind = _kind(node, env)
    if not _is_boolean(node, env):
        if kind is None:
            return None
        if kind in (_Kind.EXCEPTION, _Kind.DATA):
            return (kind, _Reach.UNKNOWN, _Reach.UNKNOWN)
        return _UNKNOWN_VALUE
    when_true, when_false = _reach(node, True, env), _reach(node, False, env)
    if _Reach.UNKNOWN in (when_true, when_false):
        return _UNKNOWN_VALUE
    return (_Kind.BOOL, when_true, when_false)


def _scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Nodes in ``node``'s own scope; a nested def is yielded but not entered."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _NEW_SCOPES):
            yield from _scope_nodes(child)


def _binds(node: ast.AST) -> set[str]:
    """The names this one node binds."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return {node.id}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    return set()


def _bound_names(node: ast.AST) -> set[str]:
    """Names bound anywhere inside ``node``'s own scope, nested excepts included."""
    names: set[str] = set()
    for child in _scope_nodes(node):
        names |= _binds(child)
    return names


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _bound_before(handler: ast.ExceptHandler, point: ast.AST) -> set[str]:
    """Names ``handler`` may bind in code that starts before ``point``."""
    names: set[str] = set()
    for node in _scope_nodes(handler):
        if _position(node) < _position(point):
            names |= _binds(node)
    return names


def _apply(stmt: ast.stmt, env: _Env) -> None:
    """Replay one earlier statement's stores into ``env``."""
    simple: tuple[str, ast.expr] | None = None
    if (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ):
        simple = (stmt.targets[0].id, stmt.value)
    elif (
        isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.value is not None
    ):
        simple = (stmt.target.id, stmt.value)
    value = _value_of(simple[1], env) if simple else None
    for name in _bound_names(stmt):
        env[name] = _UNKNOWN_VALUE
    if simple:
        name = simple[0]
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value


def _parents(handler: ast.ExceptHandler) -> _Parents:
    """Map each node to its parent, the parent's field, and its index in it."""
    parents: _Parents = {}
    for parent in [handler, *_own_nodes(handler)]:
        for field, value in ast.iter_fields(parent):
            values = value if isinstance(value, list) else [value]
            children: list[ast.AST] = [c for c in values if isinstance(c, ast.AST)]
            for index, child in enumerate(children):
                parents[child] = (parent, field, index)
    return parents


def _block(parent: ast.AST, field: str) -> list[ast.stmt]:
    for name, value in ast.iter_fields(parent):
        if name == field and isinstance(value, list):
            return [s for s in value if isinstance(s, ast.stmt)]
    return []


def _ancestry(
    node: ast.AST, parents: _Parents, handler: ast.ExceptHandler
) -> list[tuple[ast.AST, str, int]]:
    """``(parent, field, index)`` from the handler down to ``node``."""
    chain: list[tuple[ast.AST, str, int]] = []
    child = node
    while child is not handler:
        parent, field, index = parents[child]
        chain.append((parent, field, index))
        child = parent
    chain.reverse()
    return chain


def _env_at(
    point: ast.AST, caught: str | None, parents: _Parents, handler: ast.ExceptHandler
) -> _Env:
    """Tracked local values when ``point`` is evaluated.

    Every name the handler binds before ``point`` starts unknown; only the
    straight-line stores on ``point``'s own path give one a value. Entering a
    loop body makes every name that loop binds unknown again, because an
    earlier iteration may have changed it.
    """
    env: _Env = {name: _UNKNOWN_VALUE for name in _bound_before(handler, point)}
    if caught:
        env[caught] = (_Kind.EXCEPTION, _Reach.UNKNOWN, _Reach.UNKNOWN)
    for parent, field, index in _ancestry(point, parents, handler):
        if field not in _BLOCK_FIELDS:
            continue
        if parent is not handler:
            stale = set()
            for header_field, value in ast.iter_fields(parent):
                if header_field not in _BLOCK_FIELDS and isinstance(value, ast.AST):
                    stale |= _bound_names(value)
            if isinstance(parent, _LOOPS):
                stale |= _bound_names(parent)
            for name in stale:
                env[name] = _UNKNOWN_VALUE
        for stmt in _block(parent, field)[:index]:
            _apply(stmt, env)
    return env


def _exits(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], _EXITS)


def _receives(call: ast.Call, env: _Env) -> bool:
    """Whether the caught exception itself is passed to ``call``."""
    values = [*call.args, *(kw.value for kw in call.keywords if kw.arg != "cause")]
    return any(_kind(value, env) is _Kind.EXCEPTION for value in values)


def _case_reach(match: ast.Match, index: int, env: _Env) -> _Reach:
    case = match.cases[index]
    subject = _kind(match.subject, env)
    guard = _reach(case.guard, True, env) if case.guard else _Reach.FREE
    if subject is None:
        return guard
    if subject is _Kind.UNKNOWN or subject is _Kind.BOOL:
        return _Reach.UNKNOWN
    catch_all = isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None
    if catch_all and index == 0:
        return guard
    return _all(_Reach.REQUIRES, guard)


def _outcome(
    leaf: ast.Call, caught: str | None, parents: _Parents, handler: ast.ExceptHandler
) -> _Outcome:
    conditions: list[_Reach] = []
    for parent, field, index in _ancestry(leaf, parents, handler):
        if isinstance(parent, ast.Call) and field in ("args", "keywords"):
            if _receives(parent, _env_at(parent, caught, parents, handler)):
                return _Outcome.UNKNOWN
        if isinstance(parent, (ast.If, ast.While, ast.IfExp)) and field in (
            "body",
            "orelse",
        ):
            env = _env_at(parent.test, caught, parents, handler)
            conditions.append(_reach(parent.test, field == "body", env))
        if isinstance(parent, ast.Match) and field == "cases":
            env = _env_at(parent.subject, caught, parents, handler)
            conditions.append(_case_reach(parent, index, env))
        if field in _BLOCK_FIELDS:
            for earlier in _block(parent, field)[:index]:
                if not isinstance(earlier, ast.If):
                    continue
                env = _env_at(earlier.test, caught, parents, handler)
                if _exits(earlier.body):
                    conditions.append(_reach(earlier.test, False, env))
                elif _exits(earlier.orelse):
                    conditions.append(_reach(earlier.test, True, env))
    reach = _all(*conditions)
    if reach is _Reach.FREE:
        return _Outcome.UNCONDITIONAL
    if reach is _Reach.UNKNOWN:
        return _Outcome.UNKNOWN
    return _Outcome.SELECTED


def handler_outcomes(
    reg: Registry,
) -> Iterator[tuple[Source, ast.ExceptHandler, list[tuple[ast.Call, _Outcome]]]]:
    """Every broad handler on a preflight path with its auth/permission leaves."""
    checker = _Checker(reg)
    for src, method in reachable_preflight_sites(reg, follow_callbacks=True):
        for handler in iter_function_nodes(method):
            if not (isinstance(handler, ast.ExceptHandler) and _is_broad(src, handler)):
                continue
            parents = _parents(handler)
            leaves = [
                (node, _outcome(node, handler.name, parents, handler))
                for node in _own_nodes(handler)
                if isinstance(node, ast.Call)
                and _customer_rooted(checker, src, node.func)
            ]
            yield src, handler, leaves


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    for src, handler, leaves in handler_outcomes(reg):
        unconditional = [
            leaf for leaf, outcome in leaves if outcome is _Outcome.UNCONDITIONAL
        ]
        if unconditional:
            leaf = unconditional[0]
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=_F021,
                    node=handler,
                    message=(
                        f"Broad except in preflight_check builds a fixed "
                        f"{ast.unparse(leaf.func)} on a path no test on the caught "
                        "exception selects, so an empty credential or an "
                        "unreachable host reads as a customer credential or grant "
                        "problem. Classify the caught exception first "
                        "(application_sdk.errors.classify_http_exception for "
                        "httpx, or an isinstance chain) and fall back to a leaf "
                        "that does not blame the customer (InternalError when the "
                        "cause is unknown), or narrow the except clause."
                    ),
                    directives=src.directives,
                )
            )
    return findings

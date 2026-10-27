"""E020 HttpFailureToEmptyReturn — checked HTTP failure coerced to an empty success.

A guard that inspects an HTTP response for failure and then ``return``\\ s an
empty/None sentinel turns a remote API failure into an empty *successful* result:
the workflow publishes a zero/partial crawl as if it completed.  Unlike the rest
of the E-series this has no ``except``/``raise`` — it is a plain ``if`` on a
response object — so it escapes every exception-shaped rule.

Detection requires a single sub-expression that is BOTH anchored on an
HTTP-response marker (``is_success`` / ``ok`` / ``status_code``) AND failure-shaped
(a negation or failure comparison) — so it fires on ``not resp.is_success`` and
``resp.status_code != 200`` but not on ordinary ``if x is None: return None``
guards, nor on a success-shaped marker check combined with an unrelated failure
(``resp.is_success and parsed is None`` — HTTP 200 with an empty body). A bounded
status-code range wholly within 200–299 is also a success check, not a failure
comparison, and ``!=`` against a single non-2xx constant (``status_code != 401``)
is no failure test at all: its branch also takes every 2xx response. Raise a typed error instead of returning an empty sentinel.

Both polarities are covered: the failure-in-the-test → empty body
(``if not resp.is_success: return []``) and its mirror, success-in-the-test →
empty ``else`` (``if resp.is_success: ... else: return []``).

``.ok`` and ``.is_success`` are also ordinary field names on non-HTTP result
objects (a ``StageResult`` dataclass), so those two markers count only on an
owner that is plausibly an HTTP response: its name says so (``resp``), the
function reads a response-only attribute on it (``.status_code``, ``.json()``,
``.headers``), it is a parameter annotated ``…Response``, or the function binds
it from an httpx / requests / aiohttp call, an HTTP verb (``.get`` /
``.post`` / ``.request`` …) or a callee named for ``http`` / ``request``.
``status_code`` needs no such evidence.

Not reported: a bare ``return`` / ``return None`` inside a function annotated
``-> None``.  A procedure has no result to publish, so the early exit hands the
caller no sentinel; E007's logged-sentinel arm makes the same carve-out.

Exempt: a per-object skip on a *permanent* client rejection.  An empty return
is not reported when the ``if`` whose body directly holds it tests the status
against an allowlist of permanent per-object rejections
(``r.status_code in (400, 404)`` or ``r.status_code == 404``, optionally
``and``-ed with other terms, which only narrow the branch) on the same response
the failure test is about, and a warning/error-level log call earlier in that
same body says the object is skipped and passes that status as a value of its
own.  Everything else keeps firing: a bare
``if not r.is_success: return {}``, transient outcomes (no response, 408, 429,
5xx), a skip with no log or a debug/info log, a log that omits the status or
does not say it skips, a guard on another response than the failed one, an
``or``-ed guard (``r is None or r.status_code == 404``), and a range form such
as ``400 <= r.status_code < 500`` (it would also cover 401/403/408/429).
401/407 are left out because a credential rejection hits every object, so a
skip silently empties the crawl; 403 is left out because whether a permission
denial should skip or raise is a site-level judgment, not a fact the checker
can prove.  A status held in a local counts only when the alias is proven
from the guard's earlier siblings: the last binding before the
guard must be ``status = r.status_code`` (or ``r.status_code if r is not None
else None``); a named constant tuple (``in _PERMANENT``) is not followed.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from ._helpers import _is_empty_sentinel, _is_log_call_stmt

_HTTP_MARKERS: frozenset[str] = frozenset({"is_success", "ok", "status_code"})
_FAILURE_COMPARE_OPS = (ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)


#: Markers that are also ordinary field names on non-HTTP result objects
#: (``StageResult.ok``, ``Outcome.is_success``).  They count only on an owner
#: that is plausibly an HTTP response — see :func:`_is_plausible_response`.
_GENERIC_MARKERS: frozenset[str] = frozenset({"is_success", "ok"})


def _references_http_marker(
    test: ast.expr, ignored: frozenset[str] = frozenset()
) -> bool:
    """Whether *test* reads an HTTP marker on an owner not in *ignored*.

    *ignored* holds the ``ast.dump`` of owners whose ``.ok`` / ``.is_success``
    is not an HTTP marker (see :func:`_non_response_owners`).
    """
    return any(
        isinstance(n, ast.Attribute)
        and n.attr in _HTTP_MARKERS
        and not (n.attr in _GENERIC_MARKERS and ast.dump(n.value) in ignored)
        for n in ast.walk(test)
    )


# Leaf names of calls that plausibly return an HTTP response: the verbs of
# httpx / requests / aiohttp clients and their wrappers.  A leaf containing
# ``http`` or ``request`` also counts (``execute_http_get``, ``_request``).
_HTTP_CALL_VERBS: frozenset[str] = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
        "send",
        "stream",
        "fetch",
        "urlopen",
    }
)
_HTTP_CLIENT_MODULES: frozenset[str] = frozenset({"requests", "httpx", "aiohttp"})
# Attributes only an HTTP response carries: reading one on the same owner in
# the same function says the owner is a response.
_RESPONSE_ONLY_ATTRS: frozenset[str] = frozenset(
    {"status_code", "raise_for_status", "headers", "json", "reason_phrase"}
)


def _call_returns_response(value: ast.expr) -> bool:
    """Whether *value* (``await`` unwrapped) is a call that plausibly returns a response."""
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Attribute):
        leaf = func.attr
        root: ast.expr = func
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in _HTTP_CLIENT_MODULES:
            return True
    elif isinstance(func, ast.Name):
        leaf = func.id
    else:
        return False
    lowered = leaf.lower()
    return (
        lowered in _HTTP_CALL_VERBS
        or "http" in lowered
        or "request" in lowered
        or "response" in lowered
    )


def _is_plausible_response(owner: ast.expr, scope: ast.AST | None) -> bool:
    """Whether *owner* (``x`` in ``x.ok``) is plausibly an HTTP response.

    Any one of:

    - its leaf name says so (``response``, ``resp``, ``self._http_resp``);
    - *scope* (the enclosing function, or the module) reads a response-only
      attribute on it (``x.status_code``, ``x.json()``, ``x.headers`` …);
    - it is a parameter annotated ``…Response`` (``httpx.Response``,
      ``ClientResponse``);
    - *scope* binds it — ``=``, ``:=``, or ``with … as`` — from a call that
      plausibly returns a response: an httpx / requests / aiohttp call, an
      HTTP verb (``.get`` / ``.post`` / ``.request`` …), or a callee whose
      name says ``http`` / ``request`` / ``response``.
    """
    if isinstance(owner, ast.Name):
        leaf = owner.id
    elif isinstance(owner, ast.Attribute):
        leaf = owner.attr
    else:
        return False
    if "resp" in leaf.lower():
        return True
    if scope is None:
        return False
    # Compared as source text: a binding target carries a Store context that
    # ``ast.dump`` would tell apart from the Load the marker reads.
    wanted = ast.unparse(owner)
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    pending: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, nested):
            continue
        pending.extend(ast.iter_child_nodes(node))
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _RESPONSE_ONLY_ATTRS
            and ast.unparse(node.value) == wanted
        ):
            return True
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets, value = [node.optional_vars], node.context_expr
        else:
            continue
        if any(ast.unparse(t) == wanted for t in targets) and _call_returns_response(
            value
        ):
            return True
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
        owner, ast.Name
    ):
        args = scope.args
        for parameter in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            annotation = parameter.annotation
            name = (
                annotation.attr
                if isinstance(annotation, ast.Attribute)
                else annotation.id
                if isinstance(annotation, ast.Name)
                else None
            )
            if parameter.arg == owner.id and name and name.endswith("Response"):
                return True
    return False


def _non_response_owners(test: ast.expr, scope: ast.AST | None) -> frozenset[str]:
    """Owners whose ``.ok`` / ``.is_success`` in *test* is not an HTTP marker."""
    return frozenset(
        ast.dump(n.value)
        for n in ast.walk(test)
        if isinstance(n, ast.Attribute)
        and n.attr in _GENERIC_MARKERS
        and not _is_plausible_response(n.value, scope)
    )


def _int_constant(node: ast.expr) -> int | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    return None


_StatusBound = tuple[str, str, int, bool]


def _status_code_bound(
    left: ast.expr, op: ast.cmpop, right: ast.expr
) -> _StatusBound | None:
    """Normalize one comparison around ``status_code`` to a numeric bound.

    The result is ``(target, lower/upper, value, inclusive)`` where *target*
    identifies the ``status_code`` expression compared. For example,
    ``200 <= response.status_code`` becomes
    ``(<response.status_code>, "lower", 200, True)``.
    """
    if isinstance(left, ast.Attribute) and left.attr == "status_code":
        target, value, flipped = left, _int_constant(right), False
    elif isinstance(right, ast.Attribute) and right.attr == "status_code":
        target, value, flipped = right, _int_constant(left), True
    else:
        return None
    if value is None:
        return None
    if isinstance(op, (ast.Lt, ast.LtE)):
        kind = "lower" if flipped else "upper"
    elif isinstance(op, (ast.Gt, ast.GtE)):
        kind = "upper" if flipped else "lower"
    else:
        return None
    return ast.dump(target), kind, value, isinstance(op, (ast.LtE, ast.GtE))


def _compare_status_bounds(node: ast.expr) -> list[_StatusBound] | None:
    """Every link of a comparison as a ``status_code`` bound, or None."""
    if not isinstance(node, ast.Compare):
        return None
    operands = [node.left, *node.comparators]
    bounds: list[_StatusBound] = []
    for left, op, right in zip(operands, node.ops, operands[1:]):
        bound = _status_code_bound(left, op, right)
        if bound is None:
            return None
        bounds.append(bound)
    return bounds


def _bounds_within_2xx(bounds: list[_StatusBound]) -> bool:
    """Whether *bounds* all constrain ONE ``status_code`` to a range inside 2xx.

    Bounds on different responses (``a.status_code <= 200 <= b.status_code``)
    never form a range — each response is constrained only on one side.
    """
    if len({bound[0] for bound in bounds}) != 1:
        return False
    lower = [v if inclusive else v + 1 for _, k, v, inclusive in bounds if k == "lower"]
    upper = [v if inclusive else v - 1 for _, k, v, inclusive in bounds if k == "upper"]
    if not lower or not upper:
        return False
    low, high = max(lower), min(upper)
    return low >= 200 and high <= 299 and low <= high


def _split_2xx_range_terms(values: list[ast.expr]) -> list[ast.expr]:
    """Drop the and-joined terms that together bound one ``status_code`` to 2xx.

    ``r.status_code >= 200 and r.status_code < 300`` is a success range spelled
    as two comparisons; the remaining terms are returned for classification.
    When the bound terms do not form a 2xx range, *values* is returned as-is.
    """
    bound_terms: list[ast.expr] = []
    bounds: list[_StatusBound] = []
    for value in values:
        value_bounds = _compare_status_bounds(value)
        if value_bounds is not None:
            bound_terms.append(value)
            bounds.extend(value_bounds)
    if not bound_terms or not _bounds_within_2xx(bounds):
        return values
    return [value for value in values if value not in bound_terms]


def _is_bounded_2xx_status_range(node: ast.expr) -> bool:
    """Whether *node* bounds one ``status_code`` entirely to 2xx.

    Accepts a chained comparison (``200 <= r.status_code < 300``) or an
    and-joined pair (``r.status_code >= 200 and r.status_code < 300``).
    """
    bounds = _compare_status_bounds(node)
    if bounds is not None:
        return _bounds_within_2xx(bounds)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return bool(node.values) and not _split_2xx_range_terms(node.values)
    return False


def _compare_is_failure(node: ast.Compare) -> bool:
    """A failure comparison: ``!= 200``, ``>= 400``, ``is None`` / ``== None``.

    NOT failure: ``== 204`` / ``== 200`` (success) and ``is True`` — equality/
    identity against a non-None constant is a success check.  Nor is ``!=``
    against one non-2xx constant (``status_code != 401``): that branch also
    takes every 2xx response, so it is not a failure test.
    """
    operands = [node.left, *node.comparators]
    for index, (op, comparator) in enumerate(zip(node.ops, node.comparators)):
        if isinstance(op, ast.NotEq):
            status = _int_constant(comparator)
            if status is None:
                status = _int_constant(operands[index])
            if status is not None and not 200 <= status <= 299:
                continue
            return True
        if isinstance(op, _FAILURE_COMPARE_OPS):
            return True
        if isinstance(op, (ast.Is, ast.Eq)) and (
            isinstance(comparator, ast.Constant) and comparator.value is None
        ):
            return True
    return False


def _is_http_failure_predicate(
    node: ast.expr, ignored: frozenset[str] = frozenset()
) -> bool:
    """True if *node* tests an HTTP response for failure, anchored to the marker.

    The failure shape must apply to the SAME sub-expression that references the
    HTTP marker — so ``not resp.is_success`` and ``resp.status_code != 200``
    qualify, but a success-shaped marker check ANDed/ORed with an unrelated
    failure (``resp.is_success and parsed is None`` — HTTP 200 with an empty body)
    does NOT.  A bounded 2xx status-code range is a success check, not failure.
    For a BoolOp, at least one value must qualify on its own; and-joined terms
    that together bound one ``status_code`` to 2xx are a success range and are
    set aside first.
    """
    if _is_bounded_2xx_status_range(node):
        return False
    if isinstance(node, ast.BoolOp):
        values = node.values
        if isinstance(node.op, ast.And):
            values = _split_2xx_range_terms(values)
        return any(_is_http_failure_predicate(v, ignored) for v in values)
    # ``not <expr-touching-an-http-marker>`` — negation of a truthy marker.
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _references_http_marker(node.operand, ignored)
    # A comparison that both uses a failure op AND touches an HTTP marker.
    if isinstance(node, ast.Compare):
        return _compare_is_failure(node) and _references_http_marker(node, ignored)
    return False


def _is_http_success_predicate(
    node: ast.expr, ignored: frozenset[str] = frozenset()
) -> bool:
    """True if *node* is a pure HTTP *success* check (so its ``else`` is failure).

    Only unambiguous forms qualify — a bare truthy marker (``if resp.is_success`` /
    ``if resp.ok``), an equality/identity against a non-None constant on the marker
    (``status_code == 200`` / ``== 204``), or a bounded range wholly inside 2xx
    (chained, or and-joined comparisons on the same ``status_code``). Any other
    BoolOp (``resp.is_success and parsed``) is deliberately excluded: its ``else``
    also fires when the unrelated condition is falsy, so the branch is not purely
    a remote-failure path.
    """
    if isinstance(node, ast.Attribute) and node.attr in _HTTP_MARKERS:
        return _references_http_marker(node, ignored)
    if _is_bounded_2xx_status_range(node):
        return True
    if isinstance(node, ast.Compare):
        for op, comparator in zip(node.ops, node.comparators):
            if (
                isinstance(op, (ast.Eq, ast.Is))
                and isinstance(comparator, ast.Constant)
                and comparator.value is not None
            ):
                return _references_http_marker(node, ignored)
    return False


def _is_plain_else(orelse: list[ast.stmt]) -> bool:
    """A real ``else:`` block — not an ``elif`` (which parses as a single nested If).

    Restricting the orelse mirror to a plain else keeps the failure branch
    unambiguous; an ``elif`` chain is handled by ``visit_If`` recursing into it.
    """
    return bool(orelse) and not (len(orelse) == 1 and isinstance(orelse[0], ast.If))


# Status codes a source returns when it permanently rejects ONE object (bad
# request, gone, not found) — never a credential, permission or transient code.
_PERMANENT_OBJECT_REJECTIONS: frozenset[int] = frozenset({400, 404, 410})
_WARN_OR_ERROR_LOG_METHODS: frozenset[str] = frozenset(
    {"warning", "warn", "error", "exception", "critical"}
)


def _status_literals(node: ast.expr) -> list[int] | None:
    """The int literals of ``(400, 404)`` / ``[...]`` / ``{...}``, or None."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)) or not node.elts:
        return None
    values = [_int_constant(elt) for elt in node.elts]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


def _permanent_rejection_target(node: ast.expr) -> ast.Attribute | ast.Name | None:
    """The status expression a guard pins to permanent per-object rejections.

    Accepts ``<x>.status_code in (<codes>)``, ``<x>.status_code == N`` and
    ``N == <x>.status_code`` where every code is a literal in the allowlist,
    or an ``and`` with at least one such term (an ``and`` only narrows the
    branch).  A bare name (``status in (400, 404)``) is returned too; the
    caller must prove it aliases a ``status_code`` (``_aliases_status_code``).
    Anything else (``or``, ``not``, ranges, a named constant) returns None.
    """
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        for value in node.values:
            target = _permanent_rejection_target(value)
            if target is not None:
                return target
        return None
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    left, op, right = node.left, node.ops[0], node.comparators[0]
    if isinstance(op, ast.In):
        codes = _status_literals(right)
        status = left
    elif isinstance(op, ast.Eq):
        if isinstance(right, (ast.Attribute, ast.Name)):
            left, right = right, left
        code = _int_constant(right)
        codes = None if code is None else [code]
        status = left
    else:
        return None
    if isinstance(status, ast.Attribute):
        if status.attr != "status_code":
            return None
    elif not isinstance(status, ast.Name):
        return None
    if codes is None or not set(codes) <= _PERMANENT_OBJECT_REJECTIONS:
        return None
    return status


def _is_status_code_read(value: ast.expr) -> bool:
    """``<x>.status_code``, or ``<x>.status_code if <cond> else None`` (either arm)."""
    if isinstance(value, ast.Attribute):
        return value.attr == "status_code"
    if isinstance(value, ast.IfExp):
        arms = (value.body, value.orelse)
        return any(
            isinstance(a, ast.Attribute) and a.attr == "status_code" for a in arms
        ) and any(isinstance(a, ast.Constant) and a.value is None for a in arms)
    return False


def _binds(stmt: ast.stmt, name: str) -> bool:
    """Whether *stmt* (re)binds or deletes *name* anywhere inside it."""
    for n in ast.walk(stmt):
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load):
            return True
        if isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            return True
    return False


def _status_code_owner(value: ast.expr) -> ast.expr | None:
    """The response a ``status_code`` read is taken from (``<x>`` in ``<x>.status_code``)."""
    if isinstance(value, ast.Attribute) and value.attr == "status_code":
        return value.value
    if isinstance(value, ast.IfExp) and _is_status_code_read(value):
        for arm in (value.body, value.orelse):
            if isinstance(arm, ast.Attribute) and arm.attr == "status_code":
                return arm.value
    return None


def _aliased_status_owner(name: str, preceding: list[ast.stmt]) -> ast.expr | None:
    """The response whose ``status_code`` *name* provably holds at the guard, or None.

    The last statement in *preceding* (the guard's earlier siblings) that
    binds *name* must be a plain ``name = <x>.status_code`` (or the
    ``... if <cond> else None`` form); any other binding, or none, fails.
    """
    for stmt in reversed(preceding):
        if not _binds(stmt, name):
            continue
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == name
            and _is_status_code_read(stmt.value)
        ):
            return _status_code_owner(stmt.value)
        return None
    return None


def _failed_responses(test: ast.expr) -> set[str]:
    """The responses an HTTP-failure test is about (``resp`` in ``not resp.is_success``).

    Every ``<x>.is_success`` / ``<x>.ok`` / ``<x>.status_code`` in the test,
    plus a bare ``not <x>`` (the no-response arm).  Compared by AST dump, so the
    exemption only applies to a guard on the very response that failed.
    """
    out: set[str] = set()
    for n in ast.walk(test):
        if isinstance(n, ast.Attribute) and n.attr in _HTTP_MARKERS:
            out.add(ast.dump(n.value))
        elif (
            isinstance(n, ast.UnaryOp)
            and isinstance(n.op, ast.Not)
            and isinstance(n.operand, (ast.Name, ast.Attribute))
        ):
            out.add(ast.dump(n.operand))
    return out


def _message_says_skip(call: ast.Call) -> bool:
    """The log message (first argument) states the object is skipped."""
    if not call.args:
        return False
    first = call.args[0]
    parts: list[str] = []
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        parts.append(first.value)
    elif isinstance(first, ast.JoinedStr):
        parts.extend(
            v.value
            for v in first.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return "skip" in "".join(parts).lower()


def _log_carries_status(stmt: ast.stmt, status: ast.Attribute | ast.Name) -> bool:
    """*stmt* is a warning/error log that records *status* and says the object is skipped.

    The status must be passed as a value of its own — a positional or keyword
    argument that *is* the status expression, or an f-string field holding it.
    A status buried in another expression (``status_code == 404``) records
    something else and does not count.  The message must say the object is
    skipped: that is the explicit per-object contract the exemption relies on,
    since a status code alone cannot tell a per-object fetch from a
    whole-request one.
    """
    if not (_is_log_call_stmt(stmt) and isinstance(stmt, ast.Expr)):
        return False
    call = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _WARN_OR_ERROR_LOG_METHODS
    ):
        return False
    if not _message_says_skip(call):
        return False
    wanted = ast.dump(status)
    values: list[ast.expr] = [*call.args, *(kw.value for kw in call.keywords)]
    for arg in call.args:
        if isinstance(arg, ast.JoinedStr):
            values.extend(
                v.value for v in arg.values if isinstance(v, ast.FormattedValue)
            )
    return any(ast.dump(v) == wanted for v in values)


def _is_logged_permanent_rejection_skip(
    guard: ast.If, ret: ast.Return, preceding: list[ast.stmt], failed: set[str]
) -> bool:
    """Whether *ret* is the compliant per-object skip directly under *guard*.

    *ret* is a direct statement of ``guard.body`` (``_empty_returns`` only
    pairs a return with the ``if`` whose body list holds it).  All of:

    - the guard pins the status of one of the *failed* responses (the ones the
      enclosing HTTP-failure test is about) to permanent per-object rejections;
      a status held in a local must be proven an alias of that response's
      ``status_code`` from the guard's earlier siblings (*preceding*);
    - a warning/error log before *ret* in the same body passes that status as a
      value and says the object is skipped, with the alias not rebound between
      the guard and the log.
    """
    status = _permanent_rejection_target(guard.test)
    if status is None:
        return False
    if isinstance(status, ast.Name):
        owner = _aliased_status_owner(status.id, preceding)
    else:
        owner = status.value
    if owner is None or ast.dump(owner) not in failed:
        return False
    for stmt in guard.body:
        if stmt is ret:
            return False
        if isinstance(status, ast.Name) and _binds(stmt, status.id):
            return False
        if _log_carries_status(stmt, status):
            return True
    return False


_Found = tuple[ast.Return, ast.If | None, list[ast.stmt]]


def _empty_returns(
    stmts: list[ast.stmt], guard: ast.If | None, preceding: list[ast.stmt]
) -> Iterator[_Found]:
    """Every empty-sentinel return in *stmts*, with the ``if`` whose body holds it.

    The guard is the ``ast.If`` whose ``body`` list directly contains the
    return (None otherwise), so the exemption only ever reads the branch the
    return is actually in, never an ``else`` or an unrelated outer test.  Every
    nested statement list is searched, matching the previous ``ast.walk``
    reach.  *preceding* is the guard's earlier siblings in the statement list
    that holds it, the only place a status alias is looked up.
    """
    for index, stmt in enumerate(stmts):
        if isinstance(stmt, ast.Return):
            if _is_empty_sentinel(stmt.value):
                yield stmt, guard, preceding
        elif isinstance(stmt, ast.If):
            yield from _empty_returns(stmt.body, stmt, stmts[:index])
            yield from _empty_returns(stmt.orelse, None, [])
        else:
            for field in ("body", "orelse", "finalbody"):
                children = getattr(stmt, field, None)
                if isinstance(children, list):
                    yield from _empty_returns(children, None, [])
            for nested in (*getattr(stmt, "handlers", ()), *getattr(stmt, "cases", ())):
                yield from _empty_returns(nested.body, None, [])


def _is_none_return(ret: ast.Return) -> bool:
    """``return`` or ``return None`` — no value a caller could read as a result."""
    return ret.value is None or (
        isinstance(ret.value, ast.Constant) and ret.value.value is None
    )


def _branch_returns_empty(
    body: list[ast.stmt],
    failed: set[str],
    guard: ast.If | None = None,
    *,
    procedure: bool = False,
) -> ast.Return | None:
    """The first empty-sentinel return in *body* that is not an exempt skip.

    *failed* is the set of responses the enclosing failure test is about, so a
    skip is only exempt when it guards one of those.  *guard* is the ``if``
    that owns *body* when *body* is its own ``body`` list (the failure
    polarity), so a failure test that is itself an ``and``-ed
    permanent-rejection guard is judged too.  *procedure* is true inside a
    function annotated ``-> None``, where a bare ``return`` / ``return None``
    is a plain early exit with no result to publish.
    """
    for ret, owner, preceding in _empty_returns(body, guard, []):
        if procedure and _is_none_return(ret):
            continue
        if owner is not None and _is_logged_permanent_rejection_skip(
            owner, ret, preceding, failed
        ):
            continue
        return ret
    return None


class HttpFailureMixin:
    """Rule method for E020 (HTTP-failure-to-empty-return)."""

    def _check_e020(self, node: ast.If) -> None:
        function = self._function_stack[-1] if self._function_stack else None
        scope: ast.AST | None = function if function is not None else self._module
        ignored = _non_response_owners(node.test, scope)
        # A bare ``return`` / ``return None`` in a function annotated
        # ``-> None`` hands the caller no sentinel it could mistake for an
        # empty success — the same carve-out E007's logged-sentinel arm makes.
        # ``return []`` there still fires: it is a value, whatever the
        # annotation says.
        returns = function.returns if function is not None else None
        procedure = isinstance(returns, ast.Constant) and returns.value is None
        # Failure in the if-test → the body is the failure branch.
        if _is_http_failure_predicate(node.test, ignored):
            ret = _branch_returns_empty(
                node.body, _failed_responses(node.test), node, procedure=procedure
            )
            if ret is not None:
                self._emit_e020(ret)
            return
        # Success in the if-test → the else is the failure branch (polarity mirror):
        #   if resp.is_success: ...success... else: return []
        if _is_http_success_predicate(node.test, ignored) and _is_plain_else(
            node.orelse
        ):
            ret = _branch_returns_empty(
                node.orelse, _failed_responses(node.test), procedure=procedure
            )
            if ret is not None:
                self._emit_e020(ret)

    def _emit_e020(self, ret: ast.Return) -> None:
        self._add(
            "E020",
            ret,
            "checked HTTP-response failure returns an empty/None sentinel instead "
            "of raising — a remote API failure is published as an empty successful "
            "result. Raise a typed error (e.g. an AppError subclass) so the failure "
            "propagates instead of silently yielding a zero/partial result. A "
            "permanent per-object rejection inside a batch (400/404/410) is a skip, "
            "not a raise: log the status at WARNING and keep the empty return.",
        )

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
comparison. Raise a typed error instead of returning an empty sentinel.

Both polarities are covered: the failure-in-the-test → empty body
(``if not resp.is_success: return []``) and its mirror, success-in-the-test →
empty ``else`` (``if resp.is_success: ... else: return []``).
"""

from __future__ import annotations

import ast

from ._helpers import _is_empty_sentinel

_HTTP_MARKERS: frozenset[str] = frozenset({"is_success", "ok", "status_code"})
_FAILURE_COMPARE_OPS = (ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)


def _references_http_marker(test: ast.expr) -> bool:
    return any(
        isinstance(n, ast.Attribute) and n.attr in _HTTP_MARKERS for n in ast.walk(test)
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
    identity against a non-None constant is a success check.
    """
    for op, comparator in zip(node.ops, node.comparators):
        if isinstance(op, _FAILURE_COMPARE_OPS):
            return True
        if isinstance(op, (ast.Is, ast.Eq)) and (
            isinstance(comparator, ast.Constant) and comparator.value is None
        ):
            return True
    return False


def _is_http_failure_predicate(node: ast.expr) -> bool:
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
        return any(_is_http_failure_predicate(v) for v in values)
    # ``not <expr-touching-an-http-marker>`` — negation of a truthy marker.
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _references_http_marker(node.operand)
    # A comparison that both uses a failure op AND touches an HTTP marker.
    if isinstance(node, ast.Compare):
        return _compare_is_failure(node) and _references_http_marker(node)
    return False


def _is_http_success_predicate(node: ast.expr) -> bool:
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
        return True
    if _is_bounded_2xx_status_range(node):
        return True
    if isinstance(node, ast.Compare):
        for op, comparator in zip(node.ops, node.comparators):
            if (
                isinstance(op, (ast.Eq, ast.Is))
                and isinstance(comparator, ast.Constant)
                and comparator.value is not None
            ):
                return _references_http_marker(node)
    return False


def _is_plain_else(orelse: list[ast.stmt]) -> bool:
    """A real ``else:`` block — not an ``elif`` (which parses as a single nested If).

    Restricting the orelse mirror to a plain else keeps the failure branch
    unambiguous; an ``elif`` chain is handled by ``visit_If`` recursing into it.
    """
    return bool(orelse) and not (len(orelse) == 1 and isinstance(orelse[0], ast.If))


def _branch_returns_empty(body: list[ast.stmt]) -> ast.Return | None:
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Return) and _is_empty_sentinel(n.value):
                return n
    return None


class HttpFailureMixin:
    """Rule method for E020 (HTTP-failure-to-empty-return)."""

    def _check_e020(self, node: ast.If) -> None:
        # Failure in the if-test → the body is the failure branch.
        if _is_http_failure_predicate(node.test):
            ret = _branch_returns_empty(node.body)
            if ret is not None:
                self._emit_e020(ret)
            return
        # Success in the if-test → the else is the failure branch (polarity mirror):
        #   if resp.is_success: ...success... else: return []
        if _is_http_success_predicate(node.test) and _is_plain_else(node.orelse):
            ret = _branch_returns_empty(node.orelse)
            if ret is not None:
                self._emit_e020(ret)

    def _emit_e020(self, ret: ast.Return) -> None:
        self._add(
            "E020",
            ret,
            "checked HTTP-response failure returns an empty/None sentinel instead "
            "of raising — a remote API failure is published as an empty successful "
            "result. Raise a typed error (e.g. an AppError subclass) so the failure "
            "propagates instead of silently yielding a zero/partial result.",
        )

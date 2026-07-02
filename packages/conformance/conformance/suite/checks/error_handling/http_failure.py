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
(``resp.is_success and parsed is None`` — HTTP 200 with an empty body).  Raise a
typed error instead of returning an empty sentinel.

Both polarities are covered: the failure-in-the-test → empty body
(``if not resp.is_success: return []``) and its mirror, success-in-the-test →
empty ``else`` (``if resp.is_success: ... else: return []``).
"""

from __future__ import annotations

import ast

from ._helpers import _get_name

_HTTP_MARKERS: frozenset[str] = frozenset({"is_success", "ok", "status_code"})
_FAILURE_COMPARE_OPS = (ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)


def _references_http_marker(test: ast.expr) -> bool:
    return any(
        isinstance(n, ast.Attribute) and n.attr in _HTTP_MARKERS for n in ast.walk(test)
    )


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
    does NOT.  For a BoolOp, at least one value must qualify on its own.
    """
    if isinstance(node, ast.BoolOp):
        return any(_is_http_failure_predicate(v) for v in node.values)
    # ``not <expr-touching-an-http-marker>`` — negation of a truthy marker.
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _references_http_marker(node.operand)
    # A comparison that both uses a failure op AND touches an HTTP marker.
    if isinstance(node, ast.Compare):
        return _compare_is_failure(node) and _references_http_marker(node)
    return False


def _is_http_success_predicate(node: ast.expr) -> bool:
    """True if *node* is a pure HTTP *success* check (so its ``else`` is failure).

    Only the unambiguous single-atom forms qualify — a bare truthy marker
    (``if resp.is_success`` / ``if resp.ok``) or an equality/identity against a
    non-None constant on the marker (``status_code == 200`` / ``== 204``).  A
    BoolOp (``resp.is_success and parsed``) is deliberately excluded: its ``else``
    also fires when the unrelated condition is falsy, so the branch is not purely
    a remote-failure path.
    """
    if isinstance(node, ast.Attribute) and node.attr in _HTTP_MARKERS:
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


def _is_empty_sentinel(value: ast.expr | None) -> bool:
    if value is None:
        return True  # bare ``return``
    if isinstance(value, ast.Constant) and value.value in (None, "", b""):
        return True
    if isinstance(value, ast.List) and not value.elts:
        return True
    if isinstance(value, ast.Dict) and not value.keys:
        return True
    if isinstance(value, ast.Tuple) and not value.elts:
        return True
    if (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and _get_name(value.func) in ("list", "dict", "set", "tuple")
    ):
        return True
    return False


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

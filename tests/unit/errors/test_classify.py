"""Unit tests for application_sdk.errors.classify."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import causal_chain, errno_classifier
from application_sdk.errors.classify import _CAUSAL_CHAIN_LIMIT
from application_sdk.errors.leaves import AuthError, PreconditionError


@dataclass(kw_only=True)
class _AuthLeaf(AuthError):
    code: ClassVar[str] = "TEST_AUTH"
    message: str = "auth failed"


@dataclass(kw_only=True)
class _UnknownDbLeaf(PreconditionError):
    code: ClassVar[str] = "TEST_UNKNOWN_DB"
    message: str = "database does not exist"


class TestCausalChain:
    def test_yields_exc_first(self) -> None:
        exc = ValueError("x")
        assert list(causal_chain(exc)) == [exc]

    def test_follows_cause_then_context(self) -> None:
        root = ValueError("root")
        middle = RuntimeError("middle")
        middle.__cause__ = root
        top = KeyError("top")
        top.__cause__ = middle
        assert list(causal_chain(top)) == [top, middle, root]

    def test_cause_takes_precedence_over_context(self) -> None:
        cause = ValueError("cause")
        context = RuntimeError("context")
        top = KeyError("top")
        top.__cause__ = cause
        top.__context__ = context  # ignored while __cause__ is set
        assert list(causal_chain(top)) == [top, cause]

    def test_falls_back_to_context_when_no_cause(self) -> None:
        context = RuntimeError("context")
        top = KeyError("top")
        top.__context__ = context
        assert list(causal_chain(top)) == [top, context]

    def test_cycle_is_bounded(self) -> None:
        a = ValueError("a")
        b = ValueError("b")
        a.__cause__ = b
        b.__cause__ = a  # cycle
        chain = list(causal_chain(a))
        assert chain == [a, b]  # each node visited once, no infinite loop

    def test_deep_chain_capped_at_limit(self) -> None:
        head = ValueError("0")
        node = head
        for i in range(1, 200):
            nxt = ValueError(str(i))
            node.__cause__ = nxt
            node = nxt
        assert len(list(causal_chain(head))) == _CAUSAL_CHAIN_LIMIT


class TestErrnoClassifier:
    def test_empty_map_returns_none(self) -> None:
        classify = errno_classifier({})
        assert classify(Exception(1045, "would-match-if-mapped")) is None

    def test_errno_via_args0(self) -> None:
        classify = errno_classifier({1045: _AuthLeaf})
        result = classify(Exception(1045, "Access denied"))
        assert isinstance(result, _AuthLeaf)
        assert result.code == "TEST_AUTH"

    def test_errno_via_errno_attribute(self) -> None:
        classify = errno_classifier({1049: _UnknownDbLeaf})
        exc = OSError()
        exc.errno = 1049
        result = classify(exc)
        assert isinstance(result, _UnknownDbLeaf)

    def test_errno_found_deeper_in_chain(self) -> None:
        classify = errno_classifier({1045: _AuthLeaf})
        inner = Exception(1045, "denied")
        outer = RuntimeError("wrapper")
        outer.__cause__ = inner
        assert isinstance(classify(outer), _AuthLeaf)

    def test_unknown_errno_returns_none(self) -> None:
        classify = errno_classifier({1045: _AuthLeaf})
        assert classify(Exception(9999, "some other failure")) is None

    def test_no_errno_returns_none(self) -> None:
        classify = errno_classifier({1045: _AuthLeaf})
        assert classify(ValueError("no numeric code here")) is None

    def test_bool_first_arg_is_not_an_errno(self) -> None:
        # bool is an int subclass but never a real DBAPI errno.
        classify = errno_classifier({1: _AuthLeaf})
        assert classify(Exception(True)) is None

    def test_returns_a_fresh_instance_each_call(self) -> None:
        classify = errno_classifier({1045: _AuthLeaf})
        first = classify(Exception(1045))
        second = classify(Exception(1045))
        assert first is not second
        assert isinstance(first, _AuthLeaf)

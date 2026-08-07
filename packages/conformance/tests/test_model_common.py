"""Tests for the model-driven check base infra (``suite.checks._model_common``).

Covers client resolution (offline clean-skip + test override), YAML suppression
directive parsing, and suppression-aware model-finding construction.  No live
model is used — a typed stub is injected via :func:`set_client_override`.
"""

from __future__ import annotations

from typing import TypeVar

import pytest
from conformance.suite.checks._ast_common import parse_ignore_directive
from conformance.suite.checks._model_common import (
    API_KEY_ENV,
    get_client,
    make_model_finding,
    parse_yaml_directives,
    set_client_override,
)
from pydantic import BaseModel

_T = TypeVar("_T", bound=BaseModel)


class _StubClient:
    """Deterministic :class:`ModelClient` — flags text containing a sentinel."""

    def __init__(self, flags: frozenset[str]) -> None:
        self._flags = flags

    def classify(self, *, system: str, user: str, schema: type[_T]) -> _T:
        low = user.lower()
        for sentinel in self._flags:
            if sentinel.lower() in low:
                return schema(flagged=True, evidence=sentinel, explanation="stub")
        return schema(flagged=False)


@pytest.fixture(autouse=True)
def _reset_override():
    yield
    set_client_override(None)


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def test_get_client_returns_none_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No override and no API key → None (callers skip cleanly)."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    set_client_override(None)
    assert get_client() is None


def test_get_client_returns_override() -> None:
    stub = _StubClient(frozenset({"x"}))
    set_client_override(stub)
    assert get_client() is stub


# ---------------------------------------------------------------------------
# YAML directive parsing
# ---------------------------------------------------------------------------


def test_parse_yaml_directives_finds_ignore() -> None:
    text = (
        "name: redshift-miner\n"
        "short_description: mines things  # conformance: ignore[M002] reviewed copy\n"
    )
    directives = parse_yaml_directives(text)
    assert set(directives) == {2}
    assert directives[2].rule_ids == frozenset({"M002"})
    assert directives[2].justification == "reviewed copy"


def test_parse_yaml_directives_ignores_plain_comments() -> None:
    text = "name: x  # just a normal comment\n"
    assert parse_yaml_directives(text) == {}


def test_parse_ignore_directive_still_used_for_grammar() -> None:
    # Guard that the shared grammar (not a private copy) backs the YAML path.
    assert parse_ignore_directive("# conformance: ignore[M001] reason") is not None


# ---------------------------------------------------------------------------
# make_model_finding
# ---------------------------------------------------------------------------


def test_make_model_finding_carries_evidence() -> None:
    f = make_model_finding(
        rule_id="M002",
        file="atlan.yaml",
        line=3,
        message="jargon",
        evidence="FOOBAR-123",
        directives={},
    )
    assert f.evidence == "FOOBAR-123"
    assert not f.suppressed


def test_make_model_finding_suppressed_same_line() -> None:
    directives = parse_yaml_directives(
        "\n\ndesc: x  # conformance: ignore[M002] reviewed\n"
    )
    f = make_model_finding(
        rule_id="M002",
        file="atlan.yaml",
        line=3,
        message="jargon",
        evidence="x",
        directives=directives,
    )
    assert f.suppressed
    assert f.suppression_justification == "reviewed"


def test_make_model_finding_not_suppressed_for_other_rule() -> None:
    directives = parse_yaml_directives(
        "\n\ndesc: x  # conformance: ignore[M001] reviewed\n"
    )
    f = make_model_finding(
        rule_id="M002",
        file="atlan.yaml",
        line=3,
        message="jargon",
        evidence="x",
        directives=directives,
    )
    assert not f.suppressed

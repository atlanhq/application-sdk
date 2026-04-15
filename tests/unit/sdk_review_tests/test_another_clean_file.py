"""Companion clean module for the @sdk-review clean-large test."""

from __future__ import annotations

from .test_clean_large_fixture import FixtureRecord, build_fixture


def build_default_fixtures() -> list[FixtureRecord]:
    """Return a canonical set of fixtures used by several tests."""
    return [
        build_fixture("a", "Alice"),
        build_fixture("b", "Bob", extra={"tier": "silver"}),
        build_fixture("c", "Carol", extra={"tier": "gold"}),
    ]


def filter_by_tier(
    records: list[FixtureRecord], *, tier: str
) -> list[FixtureRecord]:
    """Return only records whose metadata ``tier`` matches."""
    return [r for r in records if r.metadata.get("tier") == tier]

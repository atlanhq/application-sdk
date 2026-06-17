"""Rule catalog — typed Python rule definitions, one module per ID series.

CATALOG is a MappingProxyType[str, RuleDefinition] keyed by rule ID.
O(1) lookup via get_rule(rule_id). Duplicate IDs raise ValueError at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from conformance.suite.rules.ci import RULES as _CI_RULES
from conformance.suite.rules.error_handling import RULES as _E_RULES
from conformance.suite.rules.logging import RULES as _L_RULES
from conformance.suite.rules.optimizations import RULES as _O_RULES
from conformance.suite.rules.prescriptions import RULES as _P_RULES
from conformance.suite.schema.catalog import RuleDefinition


def _combine_rules(*series: tuple[RuleDefinition, ...]) -> dict[str, RuleDefinition]:
    """Combine rule series, raising ValueError on duplicate IDs. Used by tests."""
    result: dict[str, RuleDefinition] = {}
    for rules in series:
        for rule in rules:
            if rule.id in result:
                raise ValueError(f"duplicate rule ID: {rule.id!r}")
            result[rule.id] = rule
    return result


_ALL_SERIES: tuple[tuple[RuleDefinition, ...], ...] = (
    _E_RULES,
    _L_RULES,
    _CI_RULES,
    _P_RULES,
    _O_RULES,
)


def _build_catalog() -> MappingProxyType[str, RuleDefinition]:
    return MappingProxyType(_combine_rules(*_ALL_SERIES))


CATALOG: Mapping[str, RuleDefinition] = _build_catalog()


def load_catalog() -> list[RuleDefinition]:
    """Return all rules in definition order (preserves ruleIndex for SARIF)."""
    return list(CATALOG.values())


def get_rule(rule_id: str) -> RuleDefinition:
    """O(1) lookup by rule ID. Raises KeyError if not found."""
    return CATALOG[rule_id]


def assert_registry_consistent(
    *,
    check_series: frozenset[str] | None = None,
    meta_series: frozenset[str] | None = None,
) -> None:
    """Validate sibling registries against the rule catalog (the single source).

    The catalog (``CATALOG`` / ``_ALL_SERIES``) is authoritative for which rule
    series exist.  The two sibling registries relate to it differently, so this
    is one helper with two checks rather than one equality:

    * ``check_series`` — the runner's registered checkers — must be a **subset**
      of the rule series.  A series may ship rule definitions + docs before its
      checker is implemented (the L-series today), so equality would be wrong.
    * ``meta_series`` — the doc generator's ``SeriesMeta`` prefixes — must
      **equal** the rule series: every defined series is expected to be
      documented, and an orphan ``SeriesMeta`` would render an empty doc.

    Each call site passes only the registry it owns; the other stays ``None``.
    Raises :class:`RuntimeError` on drift.
    """
    rule_series = frozenset(rule_id[0] for rule_id in CATALOG)
    if check_series is not None and not check_series <= rule_series:
        raise RuntimeError(
            "conformance registry drift: checker series "
            f"{sorted(check_series - rule_series)} have no rule definitions in "
            "the catalog (add them to conformance.suite.rules)"
        )
    if meta_series is not None and meta_series != rule_series:
        raise RuntimeError(
            "rule-doc registry drift: series with rules but no SeriesMeta="
            f"{sorted(rule_series - meta_series)}; SeriesMeta with no rules="
            f"{sorted(meta_series - rule_series)}"
        )

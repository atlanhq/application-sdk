"""Rule catalog — typed Python rule definitions, one module per ID series.

CATALOG is a MappingProxyType[str, RuleDefinition] keyed by rule ID.
O(1) lookup via get_rule(rule_id). Duplicate IDs raise ValueError at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from suite.rules.ci import RULES as _CI_RULES
from suite.rules.error_recovery import RULES as _P_RULES
from suite.rules.logging import RULES as _L_RULES
from suite.schema.catalog import RuleDefinition


def _combine_rules(*series: tuple[RuleDefinition, ...]) -> dict[str, RuleDefinition]:
    """Combine rule series, raising ValueError on duplicate IDs. Used by tests."""
    result: dict[str, RuleDefinition] = {}
    for rules in series:
        for rule in rules:
            if rule.id in result:
                raise ValueError(f"duplicate rule ID: {rule.id!r}")
            result[rule.id] = rule
    return result


_ALL_SERIES: tuple[tuple[RuleDefinition, ...], ...] = (_P_RULES, _L_RULES, _CI_RULES)


def _build_catalog() -> MappingProxyType[str, RuleDefinition]:
    return MappingProxyType(_combine_rules(*_ALL_SERIES))


CATALOG: Mapping[str, RuleDefinition] = _build_catalog()


def load_catalog() -> list[RuleDefinition]:
    """Return all rules in definition order (preserves ruleIndex for SARIF)."""
    return list(CATALOG.values())


def get_rule(rule_id: str) -> RuleDefinition:
    """O(1) lookup by rule ID. Raises KeyError if not found."""
    return CATALOG[rule_id]

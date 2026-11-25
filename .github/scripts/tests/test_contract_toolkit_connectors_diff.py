"""The Connectors.pkl vs pyatlan comparison reports every kind of drift (FND-4933).

``contract-toolkit/scripts/test-sdk-import.py`` runs ``_diff_connectors`` on
the real registry, which is in sync, so that run alone cannot show a branch
still reports a mismatch. These cases feed it synthetic drift, one kind each.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "contract-toolkit" / "scripts" / "test-sdk-import.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("toolkit_test_sdk_import", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# pyatlan's side, as {name: (value, category)}, and its categories.
SDK: dict[str, tuple[str, str]] = {
    "SNOWFLAKE": ("snowflake", "warehouse"),
    "SAP_ECC": ("sap-ecc", "erp"),
}
SDK_CATEGORIES = {"warehouse", "erp"}


def _pkl(**overrides: Any) -> dict[str, Any]:
    """Connectors.pkl's side, in sync with SDK plus the allowlisted entries."""
    connectors: dict[str, dict[str, str]] = {
        "SNOWFLAKE": {"value": "snowflake", "category": "warehouse"},
        "SAP_ECC": {"value": "sap-ecc", "category": "warehouse"},
        "UNKNOWN_CUSTOM": {"value": "(custom)", "category": "API"},
        "QLIKSENSE_ENTERPRISE_WINDOWS": {
            "value": "qlik-sense-enterprise-windows",
            "category": "bi",
        },
    }
    pkl: dict[str, Any] = {
        "connectors": connectors,
        "categories": sorted(SDK_CATEGORIES),
    }
    pkl.update(overrides)
    return pkl


def _with(changes: dict[str, dict[str, str] | None]) -> dict[str, Any]:
    connectors = dict(_pkl()["connectors"])
    for name, entry in changes.items():
        if entry is None:
            del connectors[name]
        else:
            connectors[name] = entry
    return _pkl(connectors=connectors)


def test_in_sync_registry_reports_nothing(script: ModuleType) -> None:
    assert script._diff_connectors(_pkl(), SDK, SDK_CATEGORIES) == []


@pytest.mark.parametrize(
    ("pkl", "sdk", "categories", "expected"),
    [
        pytest.param(
            _with({}),
            {**SDK, "DREMIO": ("dremio", "warehouse")},
            SDK_CATEGORIES,
            "pyatlan's AtlanConnectorType has ['DREMIO']",
            id="missing-connector",
        ),
        pytest.param(
            _with({"NEW_ONE": {"value": "new-one", "category": "warehouse"}}),
            SDK,
            SDK_CATEGORIES,
            "['NEW_ONE'] are not in pyatlan's AtlanConnectorType",
            id="unlisted-pkl-only-connector",
        ),
        pytest.param(
            _with({"UNKNOWN_CUSTOM": None}),
            SDK,
            SDK_CATEGORIES,
            "_PKL_ONLY_CONNECTORS: ['UNKNOWN_CUSTOM'] are no longer",
            id="stale-pkl-only-allowlist",
        ),
        pytest.param(
            _with({"SNOWFLAKE": {"value": "snowflakex", "category": "warehouse"}}),
            SDK,
            SDK_CATEGORIES,
            "SNOWFLAKE.value is 'snowflakex', pyatlan says 'snowflake'",
            id="value-mismatch",
        ),
        pytest.param(
            _with({"SNOWFLAKE": {"value": "snowflake", "category": "erp"}}),
            SDK,
            SDK_CATEGORIES,
            "SNOWFLAKE.category is 'erp', pyatlan says 'warehouse'",
            id="category-mismatch",
        ),
        pytest.param(
            _with({"SAP_ECC": {"value": "sap-ecc", "category": "erp"}}),
            SDK,
            SDK_CATEGORIES,
            "_KNOWN_CATEGORY_MISMATCHES: [('SAP_ECC', 'warehouse', 'erp')] no longer",
            id="stale-category-allowlist",
        ),
        pytest.param(
            _with({}),
            SDK,
            SDK_CATEGORIES | {"ai"},
            "pyatlan has ['ai'], the Connectors.pkl typealias does not",
            id="missing-category",
        ),
        pytest.param(
            _pkl(categories=sorted(SDK_CATEGORIES | {"bogus"})),
            SDK,
            SDK_CATEGORIES,
            "Connectors.pkl accepts ['bogus'], pyatlan does not",
            id="extra-category",
        ),
    ],
)
def test_each_kind_of_drift_is_reported(
    script: ModuleType,
    pkl: dict[str, Any],
    sdk: dict[str, tuple[str, str]],
    categories: set[str],
    expected: str,
) -> None:
    errors: list[str] = script._diff_connectors(pkl, sdk, categories)
    assert len(errors) == 1, errors
    assert expected in errors[0], errors

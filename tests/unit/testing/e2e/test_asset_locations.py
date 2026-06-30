"""Tests for BaseE2ETest._validate_asset_locations (qualifiedName hierarchy depth).

Pure logic — no tenant / Atlas needed; a subclass declares
``expected_asset_qn_depth`` and we call the validator directly with a fake
sample of qualifiedNames.
"""

from __future__ import annotations

from application_sdk.testing.e2e import BaseE2ETest


class _SQLHierarchy(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expect_lineage = False
    # Below the connection: db=1, schema=2, table/view=3, column=4.
    expected_asset_qn_depth = {
        "Database": 1,
        "Schema": 2,
        "Table": 3,
        "Column": 4,
    }


class _NoLocationCheck(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expect_lineage = False


def _make(cls: type[BaseE2ETest], conn_qn: str = "default/x/123") -> BaseE2ETest:
    inst = cls()
    # setup_method (which derives this) isn't run in pure unit tests.
    inst.connection_qualified_name = conn_qn
    return inst


def test_correct_hierarchy_passes() -> None:
    inst = _make(_SQLHierarchy)
    samples = {
        "Database": ["default/x/123/db"],
        "Schema": ["default/x/123/db/sch"],
        "Table": ["default/x/123/db/sch/tbl", "default/x/123/db/sch/tbl2"],
        "Column": ["default/x/123/db/sch/tbl/col"],
    }
    assert inst._validate_asset_locations(samples) == []


def test_wrong_depth_flags_the_type() -> None:
    inst = _make(_SQLHierarchy)
    # Table is missing the schema segment -> 2 below the connection, expected 3.
    samples = {"Table": ["default/x/123/db/tbl"]}
    failures = inst._validate_asset_locations(samples)
    assert len(failures) == 1
    assert "Table" in failures[0]
    assert "expected 3" in failures[0]


def test_asset_not_under_connection_flags() -> None:
    inst = _make(_SQLHierarchy)
    # Right depth but wrong connection prefix (e.g. app-name / epoch drift).
    samples = {"Database": ["default/y/999/db"]}
    failures = inst._validate_asset_locations(samples)
    assert len(failures) == 1
    assert "not nested under the connection" in failures[0]


def test_empty_samples_for_a_type_are_skipped() -> None:
    inst = _make(_SQLHierarchy)
    # No Table assets sampled -> not this check's job (count floors cover it).
    samples = {"Database": ["default/x/123/db"], "Table": []}
    assert inst._validate_asset_locations(samples) == []


def test_no_expected_depth_is_a_noop() -> None:
    inst = _make(_NoLocationCheck)
    # Even obviously-wrong samples pass when the connector opts out.
    samples = {"Table": ["totally/wrong/place"]}
    assert inst._validate_asset_locations(samples) == []


def test_multiple_bad_assets_all_reported() -> None:
    inst = _make(_SQLHierarchy)
    samples = {
        "Schema": ["default/x/123/db/sch", "default/x/123/sch_flat"],  # 2nd is depth 1
        "Column": ["default/x/123/db/sch/tbl/col/extra"],  # depth 5
    }
    failures = inst._validate_asset_locations(samples)
    assert len(failures) == 2

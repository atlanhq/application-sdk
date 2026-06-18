"""AE taxonomy additions: the two new categories + the Stage / LineageStatus enums."""

from application_sdk.observability.lineage import (
    ARS_REASON_CODES,
    LineageStatus,
    ReasonCategory,
    ReasonCodeRegistry,
    Stage,
)


def test_two_new_categories_exist():
    assert ReasonCategory.OBSERVABILITY_DISABLED == "OBSERVABILITY_DISABLED"
    assert ReasonCategory.UNINSTRUMENTED_PATH == "UNINSTRUMENTED_PATH"


def test_category_set_frozen_at_twelve():
    assert len(list(ReasonCategory)) == 12


def test_stage_dimension_values():
    assert {s.value for s in Stage} == {"transform", "publish", "end_to_end"}


def test_lineage_status_values():
    assert {s.value for s in LineageStatus} == {
        "HAS_LINEAGE",
        "PARTIAL_LINEAGE",
        "MISSING_LINEAGE",
        "PENDING",
    }


def test_ars_registry_codes_map_to_valid_categories():
    assert ARS_REASON_CODES, "ARS registry must not be empty"
    for code, rc in ARS_REASON_CODES.items():
        assert code == rc.code
        assert isinstance(rc.category, ReasonCategory)


def test_registry_warns_and_defaults_on_unregistered_code(caplog):
    registry = ReasonCodeRegistry(ARS_REASON_CODES)
    assert registry.category("ARS_EDGE_DROPPED") == ReasonCategory.ASSET_NOT_FOUND
    cat = registry.category("TOTALLY_UNKNOWN_CODE")
    assert cat == ReasonCategory.UNINSTRUMENTED_PATH
    assert any("unregistered" in r.message.lower() for r in caplog.records)


def test_registry_rejects_key_code_mismatch():
    from application_sdk.observability.lineage import ReasonCode

    bad = {"WRONG_KEY": ReasonCode("ACTUAL_CODE", ReasonCategory.CACHE_MISS, "x")}
    try:
        ReasonCodeRegistry(bad)
        assert False, "expected ValueError on key/code mismatch"
    except ValueError:
        pass

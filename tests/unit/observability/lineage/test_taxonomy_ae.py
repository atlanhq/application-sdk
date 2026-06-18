"""AE taxonomy additions: the two new categories + the Stage / LineageStatus enums."""

from application_sdk.observability.lineage import (
    LineageStatus,
    ReasonCategory,
    ReasonCode,
    ReasonCodeRegistry,
    Stage,
)

# Connector-style subcode fixture (the SDK ships no concrete registry of its own).
_FIXTURE = {
    "EXAMPLE_DROPPED": ReasonCode(
        "EXAMPLE_DROPPED", ReasonCategory.ASSET_NOT_FOUND, "dropped"
    ),
}


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


def test_registry_codes_map_to_valid_categories():
    registry = ReasonCodeRegistry(_FIXTURE)
    assert len(registry) == 1
    assert registry.category("EXAMPLE_DROPPED") == ReasonCategory.ASSET_NOT_FOUND


def test_registry_warns_and_defaults_on_unregistered_code(caplog):
    registry = ReasonCodeRegistry(_FIXTURE)
    cat = registry.category("TOTALLY_UNKNOWN_CODE")
    assert cat == ReasonCategory.UNINSTRUMENTED_PATH
    assert any("unregistered" in r.message.lower() for r in caplog.records)


def test_registry_rejects_key_code_mismatch():
    bad = {"WRONG_KEY": ReasonCode("ACTUAL_CODE", ReasonCategory.CACHE_MISS, "x")}
    try:
        ReasonCodeRegistry(bad)
        assert False, "expected ValueError on key/code mismatch"
    except ValueError:
        pass

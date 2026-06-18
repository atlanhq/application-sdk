"""Generic taxonomy-machinery tests (SDK owns categories + machinery, not subcodes).

The SDK ships NO concrete reason-code registry, so these tests exercise
``ReasonCategory`` / ``ReasonCode`` / ``ObservabilityConfig`` / ``get_reason_category``
against a small local fixture registry that stands in for a connector's subcodes.
"""

from application_sdk.observability.lineage import (
    ObservabilityConfig,
    ReasonCategory,
    ReasonCode,
    get_reason_category,
)

# A connector-style subcode registry fixture (the SDK ships none of its own).
_FIXTURE_REASON_CODES = {
    "EXAMPLE_CACHE_MISS": ReasonCode(
        "EXAMPLE_CACHE_MISS", ReasonCategory.CACHE_MISS, "source not in cache"
    ),
    "EXAMPLE_NO_UPSTREAM": ReasonCode(
        "EXAMPLE_NO_UPSTREAM", ReasonCategory.NO_UPSTREAM_DATA, "no upstream refs"
    ),
}


def test_reason_category_values():
    """All argo categories still exist (locked surface)."""
    assert ReasonCategory.PARSER_FAILURE == "PARSER_FAILURE"
    assert ReasonCategory.ASSET_NOT_FOUND == "ASSET_NOT_FOUND"
    assert ReasonCategory.CACHE_MISS == "CACHE_MISS"
    assert ReasonCategory.MISSING_METADATA == "MISSING_METADATA"
    assert ReasonCategory.NO_UPSTREAM_DATA == "NO_UPSTREAM_DATA"
    assert ReasonCategory.NO_SEARCHABLE_DIALECT == "NO_SEARCHABLE_DIALECT"
    assert ReasonCategory.PROCESS_MAPPING_FAILURE == "PROCESS_MAPPING_FAILURE"
    assert ReasonCategory.CONNECTOR_LIMITATION == "CONNECTOR_LIMITATION"
    assert ReasonCategory.RELATIONSHIP_FALLBACK == "RELATIONSHIP_FALLBACK"
    assert ReasonCategory.UNSUPPORTED_SOURCE_FEATURE == "UNSUPPORTED_SOURCE_FEATURE"


def test_reason_code_frozen():
    """ReasonCode is immutable."""
    rc = ReasonCode(code="TEST", category=ReasonCategory.CACHE_MISS, message="test")
    assert rc.code == "TEST"
    assert rc.category == ReasonCategory.CACHE_MISS
    try:
        rc.code = "OTHER"
        assert False, "Should have raised"
    except AttributeError:
        pass


def test_observability_config_defaults():
    config = ObservabilityConfig()
    assert config.enabled is True
    assert config.log_successful_lineage is False


def test_registry_keys_match_code_field():
    """Every registry key matches its ReasonCode.code field."""
    for key, reason_code in _FIXTURE_REASON_CODES.items():
        assert key == reason_code.code


def test_registry_codes_have_valid_categories():
    for key, reason_code in _FIXTURE_REASON_CODES.items():
        assert isinstance(
            reason_code.category, ReasonCategory
        ), f"{key} invalid category"


def test_get_reason_category_found():
    assert (
        get_reason_category("EXAMPLE_CACHE_MISS", _FIXTURE_REASON_CODES)
        == ReasonCategory.CACHE_MISS
    )


def test_get_reason_category_not_found():
    assert get_reason_category("NONEXISTENT_CODE", _FIXTURE_REASON_CODES) is None


def test_get_reason_category_no_registry():
    assert get_reason_category("EXAMPLE_CACHE_MISS", None) is None

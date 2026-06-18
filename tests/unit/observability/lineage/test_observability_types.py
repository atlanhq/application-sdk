from application_sdk.observability.lineage import (
    ObservabilityConfig,
    ReasonCategory,
    ReasonCode,
    TABLEAU_REASON_CODES,
    get_reason_category,
)


def test_reason_category_values():
    """All expected categories exist."""
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


def test_tableau_reason_codes_registry_not_empty():
    assert len(TABLEAU_REASON_CODES) >= 30


def test_tableau_reason_codes_keys_match_code_field():
    """Every registry key matches its ReasonCode.code field."""
    for key, reason_code in TABLEAU_REASON_CODES.items():
        assert key == reason_code.code, f"Mismatch: key={key} code={reason_code.code}"


def test_tableau_reason_codes_all_have_valid_categories():
    """Every ReasonCode has a valid ReasonCategory."""
    for key, reason_code in TABLEAU_REASON_CODES.items():
        assert isinstance(
            reason_code.category, ReasonCategory
        ), f"{key} has invalid category"


def test_get_reason_category_found():
    cat = get_reason_category("CONNECTION_CACHE_MISS", TABLEAU_REASON_CODES)
    assert cat == ReasonCategory.CACHE_MISS


def test_get_reason_category_not_found():
    cat = get_reason_category("NONEXISTENT_CODE", TABLEAU_REASON_CODES)
    assert cat is None


def test_get_reason_category_no_registry():
    cat = get_reason_category("CONNECTION_CACHE_MISS", None)
    assert cat is None


def test_known_category_mappings():
    """Spot-check known Tableau codes map to expected categories."""
    checks = {
        "CUSTOM_SQL_PARSER_FAILURE": ReasonCategory.PARSER_FAILURE,
        "CONNECTION_CACHE_MISS": ReasonCategory.CACHE_MISS,
        "FIELD_CONNECTION_CACHE_MISS": ReasonCategory.CACHE_MISS,
        "DATASOURCE_MISSING_SITE_ID": ReasonCategory.MISSING_METADATA,
        "DATASOURCE_HAS_NO_UPSTREAM_TABLES": ReasonCategory.NO_UPSTREAM_DATA,
        "NO_SEARCHABLE_DIALECT": ReasonCategory.NO_SEARCHABLE_DIALECT,
        "MISSING_DATASOURCE_TO_WORKSHEET_PROCESS": ReasonCategory.PROCESS_MAPPING_FAILURE,
        "DASHBOARD_UNPUBLISHED_UPSTREAM_DISABLED": ReasonCategory.CONNECTOR_LIMITATION,
        "WORKBOOK_NO_EMBEDDED_DATASOURCE_RELATIONSHIP": ReasonCategory.RELATIONSHIP_FALLBACK,
        "DATASOURCE_UPSTREAM_DATASOURCE_NOT_FOUND_NO_SQL_FALLBACK": ReasonCategory.ASSET_NOT_FOUND,
    }
    for code, expected_category in checks.items():
        assert (
            TABLEAU_REASON_CODES[code].category == expected_category
        ), f"{code} mismatch"

"""Tests that validate produced SARIF documents against the vendored schema.

Covers:
- The golden four-disposition fixture validates against sarif-schema-2.1.0.json
- A ReportBuilder-produced document validates
- A deliberately malformed document raises jsonschema.ValidationError
"""

import json
from pathlib import Path

import jsonschema
import pytest
from suite.schema import ReportBuilder, load_catalog, validate_sarif
from suite.schema.sarif import Suppression

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Golden example
# ---------------------------------------------------------------------------


def test_golden_example_validates() -> None:
    """The committed golden fixture must pass SARIF 2.1.0 schema validation."""
    golden = json.loads(
        (_FIXTURES / "golden_four_dispositions.sarif.json").read_text(encoding="utf-8")
    )
    # Should not raise
    validate_sarif(golden)


def test_golden_example_has_four_dispositions() -> None:
    """The golden fixture must contain exactly the four expected dispositions."""
    from suite.schema import Disposition, SarifReport, derive_disposition

    data = json.loads(
        (_FIXTURES / "golden_four_dispositions.sarif.json").read_text(encoding="utf-8")
    )
    report = SarifReport.model_validate(data)
    results = report.runs[0].results

    dispositions = [derive_disposition(r) for r in results]
    assert Disposition.PASS in dispositions
    assert Disposition.FAILING in dispositions
    assert Disposition.WARNING in dispositions
    assert Disposition.SUPPRESSED in dispositions


def test_golden_example_exit_code() -> None:
    """The golden fixture has exitCode=1 because it contains a FAILING result."""
    data = json.loads(
        (_FIXTURES / "golden_four_dispositions.sarif.json").read_text(encoding="utf-8")
    )
    invocations = data["runs"][0]["invocations"]
    assert invocations[0]["exitCode"] == 1


def test_golden_example_summary() -> None:
    """The golden fixture summary counts match the four dispositions."""
    data = json.loads(
        (_FIXTURES / "golden_four_dispositions.sarif.json").read_text(encoding="utf-8")
    )
    summary = data["runs"][0]["properties"]["atlan/summary"]
    assert summary["passing"] == 1
    assert summary["failing"] == 1
    assert summary["warning"] == 1
    assert summary["suppressed"] == 1


# ---------------------------------------------------------------------------
# ReportBuilder produces valid SARIF
# ---------------------------------------------------------------------------


def test_report_builder_empty_validates() -> None:
    """An empty report (no results) is still valid SARIF."""
    builder = ReportBuilder(tool_name="atlan-conformance", tool_version="3.16.0")
    report = builder.build()
    validate_sarif(report)


def test_report_builder_with_results_validates() -> None:
    """A report with one failing result validates and has exitCode=1."""
    catalog = load_catalog()
    builder = ReportBuilder.from_catalog(
        catalog,
        tool_name="atlan-conformance",
        tool_version="3.16.0",
        repo_uri="https://github.com/atlanhq/test-app",
        commit_sha="abc123",
        branch="main",
    )
    builder.add_result(
        rule_id="P001",
        file_uri="src/connector/extractor.py",
        start_line=42,
        message="Bare 'except: pass' silently discards every exception",
        snippet="except:\n    pass",
    )
    report = builder.build()
    validate_sarif(report)

    doc = report.model_dump(by_alias=True, exclude_none=True)
    assert doc["runs"][0]["invocations"][0]["exitCode"] == 1


def test_report_builder_suppressed_result_validates() -> None:
    """A report with only a suppressed result validates and has exitCode=0."""
    catalog = load_catalog()
    builder = ReportBuilder.from_catalog(
        catalog,
        tool_name="atlan-conformance",
        tool_version="3.16.0",
    )
    suppression = Suppression(
        kind="inSource",
        justification="optional-dep guard: acceptable here",
    )
    builder.add_result(
        rule_id="P001",
        file_uri="src/connector/extractor.py",
        start_line=87,
        suppressions=[suppression],
    )
    report = builder.build()
    validate_sarif(report)

    doc = report.model_dump(by_alias=True, exclude_none=True)
    assert doc["runs"][0]["invocations"][0]["exitCode"] == 0


def test_report_builder_pass_only_validates() -> None:
    """A report with only a pass result validates and has exitCode=0."""
    builder = ReportBuilder(tool_name="atlan-conformance", tool_version="3.16.0")
    builder.add_pass(rule_id="L001", file_uri="src/connector/loader.py")
    report = builder.build()
    validate_sarif(report)

    doc = report.model_dump(by_alias=True, exclude_none=True)
    assert doc["runs"][0]["invocations"][0]["exitCode"] == 0


def test_report_builder_warning_only_exit_code() -> None:
    """A report with only warning-tier (warn) results has exitCode=0."""
    catalog = load_catalog()
    builder = ReportBuilder.from_catalog(
        catalog,
        tool_name="atlan-conformance",
        tool_version="3.16.0",
    )
    # L006 (InfoInTightLoop) is tier=warn → level=warning
    builder.add_result(
        rule_id="L006",
        file_uri="src/connector/extractor.py",
        start_line=55,
        level="warning",
        message="logger.info() inside a tight loop",
    )
    report = builder.build()
    doc = report.model_dump(by_alias=True, exclude_none=True)
    assert doc["runs"][0]["invocations"][0]["exitCode"] == 0


# ---------------------------------------------------------------------------
# Validation catches malformed documents
# ---------------------------------------------------------------------------


def test_validation_catches_missing_version() -> None:
    """A document missing the required 'version' field fails validation."""
    bad_doc = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "runs": [],
        # 'version' deliberately omitted
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sarif(bad_doc)


def test_validation_catches_invalid_level() -> None:
    """A result with an invalid level value fails validation."""
    bad_doc = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "test"}},
                "results": [
                    {
                        "ruleId": "P001",
                        "level": "critical",  # not a valid SARIF level
                        "message": {"text": "test"},
                    }
                ],
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sarif(bad_doc)

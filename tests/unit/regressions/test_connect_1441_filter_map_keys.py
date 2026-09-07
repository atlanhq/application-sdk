"""CONNECT-1441 regression coverage for literal names with consecutive hyphens."""

from application_sdk.templates.contracts.sql_metadata import ExtractionInput


def test_structured_filter_map_accepts_literal_key_with_consecutive_hyphens() -> None:
    """AE map keys are identifiers, not raw SQL fragments."""
    exclude_filter = {"^omnicom-id---versuni$": []}

    result = ExtractionInput.model_validate({"exclude_filter": exclude_filter})

    assert result.exclude_filter == exclude_filter

"""An empty picker sent as JSON text ``"[]"`` is no filter (FND-4559).

The FND-501 submission check compiles a string filter as a regex, and ``[]`` is
an unterminated character class in every dialect, so ``"[]"`` was rejected.
Accepting it is not enough: a string filter is rendered into extraction SQL
verbatim, so it must also be read as no filter everywhere it is consumed.
"""

from __future__ import annotations

import pytest

from application_sdk.common.sql_filters import (
    normalize_empty_json_list_filter,
    parse_filter_input,
    prepare_filters,
    validate_filter_regex_syntax,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
    TreeSelectionExtractionInput,
)

EMPTY_LISTS = ["[]", " [] ", "\n[]\t"]


@pytest.mark.parametrize("empty", EMPTY_LISTS)
def test_empty_json_list_is_no_filter(empty: str) -> None:
    assert normalize_empty_json_list_filter(empty) == ""


@pytest.mark.parametrize(
    "value", ["", "[A-Z]+", "[12]", "[ ]", "{}", "^prod$", None, {"^db$": []}]
)
def test_other_values_are_unchanged(value: object) -> None:
    """A bracketed regex is a regex; only the empty list means no filter."""
    assert normalize_empty_json_list_filter(value) == value


@pytest.mark.parametrize(
    "model", [ExtractionInput, ExtractionTaskInput, TreeSelectionExtractionInput]
)
@pytest.mark.parametrize("field", ["include_filter", "exclude_filter"])
def test_input_models_store_empty_json_list_as_no_filter(
    model: type[ExtractionInput], field: str
) -> None:
    validated = model.model_validate({field: "[]"})
    assert getattr(validated, field) == ""


@pytest.mark.parametrize("field", ["include_filter", "exclude_filter"])
def test_invalid_bracket_regex_is_still_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="unterminated character class"):
        ExtractionTaskInput.model_validate({field: "[a-"})


def test_raw_empty_list_is_still_not_a_valid_regex() -> None:
    """The regex check is unchanged; ``"[]"`` is normalised before it runs."""
    with pytest.raises(ValueError, match="unterminated character class"):
        validate_filter_regex_syntax("[]")


def test_parse_filter_input_reads_empty_list_as_empty_mapping() -> None:
    assert parse_filter_input("[]") == {}


def test_prepare_filters_treats_empty_list_as_no_filter() -> None:
    assert prepare_filters("[]", "[]") == (".*", "^$")

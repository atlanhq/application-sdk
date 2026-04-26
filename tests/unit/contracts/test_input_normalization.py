"""Unit tests for Input._normalize_input_payload.

Covers:
- Metadata dict lifting to top level
- Kebab-case key conversion to snake_case
- JSON string parsing for dict/list values
- String boolean coercion
- Opt-out via normalize_input = False
- Metadata does not override existing top-level keys
- ConnectionRef.qualified_name property
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import ConfigDict

from application_sdk.contracts.base import Input
from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FlexInput(Input, allow_unbounded_fields=True):
    """Permissive Input subclass used by normalization tests.

    ``allow_unbounded_fields=True`` silences payload-safety validation so
    we can pass arbitrary fields without annotating them.  ``extra="allow"``
    lets Pydantic accept keys that aren't declared as model fields.
    """

    model_config = ConfigDict(extra="allow")


class _NoNormInput(Input, allow_unbounded_fields=True):
    """Input subclass that opts out of normalization."""

    normalize_input: ClassVar[bool] = False

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Metadata lifting
# ---------------------------------------------------------------------------


class TestMetadataLifting:
    def test_metadata_dict_lifted_to_top_level(self) -> None:
        data = {"metadata": {"include_filter": ".*", "batch_size": "10"}}
        obj = _FlexInput.model_validate(data)
        # Fields from metadata should be accessible at top level
        assert obj.__pydantic_extra__["include_filter"] == ".*"

    def test_metadata_does_not_override_existing(self) -> None:
        data = {"workflow_id": "original", "metadata": {"workflow_id": "override"}}
        obj = _FlexInput.model_validate(data)
        assert obj.workflow_id == "original"

    def test_metadata_non_dict_ignored(self) -> None:
        """metadata that is not a dict should not be lifted."""
        data = {"metadata": "just-a-string"}
        obj = _FlexInput.model_validate(data)
        # Should survive as-is, not cause an error
        assert obj.__pydantic_extra__["metadata"] == "just-a-string"


# ---------------------------------------------------------------------------
# Kebab-case conversion
# ---------------------------------------------------------------------------


class TestKebabCaseConversion:
    def test_kebab_to_snake(self) -> None:
        data = {"include-filter": ".*"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["include_filter"] == ".*"

    def test_kebab_original_preserved(self) -> None:
        """Original kebab-case key is kept for backward compatibility."""
        data = {"include-filter": ".*"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["include-filter"] == ".*"

    def test_kebab_does_not_override_existing_snake(self) -> None:
        data = {"include_filter": "existing", "include-filter": "kebab"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["include_filter"] == "existing"


# ---------------------------------------------------------------------------
# JSON string parsing
# ---------------------------------------------------------------------------


class TestJsonStringParsing:
    def test_json_dict_string_parsed(self) -> None:
        payload = json.dumps({"qualifiedName": "default/sf/123"})
        data = {"connection": payload}
        obj = _FlexInput.model_validate(data)
        assert isinstance(obj.__pydantic_extra__["connection"], dict)
        assert obj.__pydantic_extra__["connection"]["qualifiedName"] == "default/sf/123"

    def test_json_list_string_parsed(self) -> None:
        payload = json.dumps(["a", "b", "c"])
        data = {"items": payload}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["items"] == ["a", "b", "c"]

    def test_invalid_json_string_kept(self) -> None:
        data = {"bad_json": "{not valid json"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["bad_json"] == "{not valid json"

    def test_non_json_string_untouched(self) -> None:
        data = {"plain": "hello world"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["plain"] == "hello world"


# ---------------------------------------------------------------------------
# Bool coercion
# ---------------------------------------------------------------------------


class TestBoolCoercion:
    def test_true_string(self) -> None:
        data = {"flag": "true"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["flag"] is True

    def test_false_string(self) -> None:
        data = {"flag": "false"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["flag"] is False

    def test_case_insensitive(self) -> None:
        data = {"a": "TRUE", "b": "False", "c": "TRUE"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["a"] is True
        assert obj.__pydantic_extra__["b"] is False
        assert obj.__pydantic_extra__["c"] is True

    def test_non_bool_string_untouched(self) -> None:
        data = {"name": "trueman"}
        obj = _FlexInput.model_validate(data)
        assert obj.__pydantic_extra__["name"] == "trueman"


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------


class TestNormalizeOptOut:
    def test_normalization_skipped(self) -> None:
        data = {"include-filter": ".*", "flag": "true"}
        obj = _NoNormInput.model_validate(data)
        # Kebab key NOT converted — normalization was skipped
        assert obj.__pydantic_extra__["include-filter"] == ".*"
        assert "include_filter" not in (obj.__pydantic_extra__ or {})
        # Bool NOT coerced
        assert obj.__pydantic_extra__["flag"] == "true"


# ---------------------------------------------------------------------------
# Combined normalization
# ---------------------------------------------------------------------------


class TestCombinedNormalization:
    def test_all_steps_together(self) -> None:
        data = {
            "metadata": {"source-tag": "v1"},
            "include-filter": '{"db": ["schema"]}',
            "enabled": "true",
        }
        obj = _FlexInput.model_validate(data)
        # metadata lifted
        assert obj.__pydantic_extra__["source_tag"] == "v1"
        # kebab converted
        assert obj.__pydantic_extra__["include_filter"] == {"db": ["schema"]}
        # JSON parsed on the kebab-converted key too
        assert isinstance(obj.__pydantic_extra__["include_filter"], dict)
        # bool coerced
        assert obj.__pydantic_extra__["enabled"] is True


# ---------------------------------------------------------------------------
# ConnectionRef.qualified_name
# ---------------------------------------------------------------------------


class TestConnectionRefQualifiedName:
    def test_qualified_name_from_attributes(self) -> None:
        ref = ConnectionRef.model_validate(
            {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "default/snowflake/123"},
            }
        )
        assert ref.qualified_name == "default/snowflake/123"

    def test_qualified_name_default_empty(self) -> None:
        ref = ConnectionRef()
        assert ref.qualified_name == ""

    def test_qualified_name_snake_case_construction(self) -> None:
        attrs = ConnectionAttributes(qualified_name="default/pg/456")
        ref = ConnectionRef(attributes=attrs)
        assert ref.qualified_name == "default/pg/456"

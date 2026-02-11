"""
Unit tests for the automation_activity decorator.

Tests focus on validating what gets added to the ACTIVITY_SPECS global,
including happy paths, validation errors, annotation handling, $defs hoisting,
and the flush_activity_registrations function.
"""

import unittest
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import BaseModel, Field

from application_sdk.decorators.automation_activity import (
    ACTIVITY_SPECS,
    automation_activity,
    flush_activity_registrations,
    isolated_activity_specs,
)
from application_sdk.decorators.automation_activity.models import (
    ActivityCategory,
    Annotation,
    Parameter,
    SubType,
    ToolMetadata,
    ToolRegistrationRequest,
    ToolSpec,
)
from application_sdk.decorators.automation_activity.registration import (
    _request_with_retry,
    _resolve_app_qualified_name,
    _resolve_automation_engine_api_url,
    _validate_base_url,
)
from application_sdk.decorators.automation_activity.schema import (
    _build_tool_spec,
    _extract_and_hoist_defs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

try:
    import jsonschema

    def assert_valid_json_schema(schema: dict) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)

    def assert_data_validates(schema: dict, data: dict) -> None:
        jsonschema.validate(data, schema)

    def assert_data_fails_validation(schema: dict, data: dict) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data, schema)

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

    def assert_valid_json_schema(schema: dict) -> None:  # type: ignore[misc]
        pass

    def assert_data_validates(schema: dict, data: dict) -> None:  # type: ignore[misc]
        pass

    def assert_data_fails_validation(schema: dict, data: dict) -> None:  # type: ignore[misc]
        pass


# Simple test Pydantic model for testing model return types
class _TestOutputModel(BaseModel):
    """Test Pydantic model for testing model return types."""

    result: str = Field(description="Test result")
    count: int = Field(default=0, description="Test count")


# =============================================================================
# Decorator tests
# =============================================================================


class TestAutomationActivity(unittest.TestCase):
    """Tests for the @automation_activity decorator."""

    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    # -- Happy paths --

    def test_single_function_registration(self):
        @automation_activity(
            display_name="Test Activity",
            description="Test activity",
            inputs=[
                Parameter(
                    name="x",
                    description="Input x",
                    annotations=Annotation(display_name="X"),
                )
            ],
            outputs=[
                Parameter(
                    name="result",
                    description="Result",
                    annotations=Annotation(display_name="Result"),
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(x: str) -> str:
            return x

        self.assertEqual(len(ACTIVITY_SPECS), 1)
        spec = ACTIVITY_SPECS[0]
        self.assertEqual(spec.display_name, "Test Activity")
        self.assertEqual(spec.description, "Test activity")
        self.assertIsNotNone(spec.input_schema)
        self.assertIsNotNone(spec.output_schema)
        assert_valid_json_schema(spec.input_schema)
        assert_valid_json_schema(spec.output_schema)

    def test_multiple_functions_accumulate(self):
        @automation_activity(
            display_name="First",
            description="First",
            inputs=[
                Parameter(
                    name="x", description="X", annotations=Annotation(display_name="X")
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def func1(x: str) -> str:
            return x

        @automation_activity(
            display_name="Second",
            description="Second",
            inputs=[
                Parameter(
                    name="y", description="Y", annotations=Annotation(display_name="Y")
                )
            ],
            outputs=[
                Parameter(
                    name="o", description="O", annotations=Annotation(display_name="O")
                )
            ],
            category=ActivityCategory.DATA,
        )
        def func2(y: int) -> int:
            return y

        self.assertEqual(len(ACTIVITY_SPECS), 2)
        self.assertEqual(ACTIVITY_SPECS[0].display_name, "First")
        self.assertEqual(ACTIVITY_SPECS[1].display_name, "Second")

    def test_function_without_inputs_outputs(self):
        @automation_activity(
            display_name="No IO",
            description="No IO",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)
        spec = ACTIVITY_SPECS[0]
        self.assertEqual(len(spec.input_schema.get("properties", {})), 0)
        self.assertEqual(spec.input_schema["input_order"], [])
        self.assertEqual(spec.output_schema["output_order"], [])

    def test_decorator_preserves_function(self):
        @automation_activity(
            display_name="Preserve",
            description="Preserve",
            inputs=[
                Parameter(
                    name="x", description="X", annotations=Annotation(display_name="X")
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(x: str) -> str:
            return f"result_{x}"

        self.assertEqual(test_func("test"), "result_test")

    # -- Schema structure tests --

    def test_input_schema_structure(self):
        @automation_activity(
            display_name="Schema",
            description="Schema",
            inputs=[
                Parameter(
                    name="x", description="X", annotations=Annotation(display_name="X")
                ),
                Parameter(
                    name="y", description="Y", annotations=Annotation(display_name="Y")
                ),
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(x: str, y: int) -> str:
            return f"{x}_{y}"

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.input_schema)
        assert_data_validates(spec.input_schema, {"x": "test", "y": 42})
        assert_data_fails_validation(spec.input_schema, {"x": "test"})
        self.assertEqual(spec.input_schema["input_order"], ["x", "y"])

    def test_output_schema_tuple_structure(self):
        @automation_activity(
            display_name="Tuple",
            description="Tuple",
            inputs=[
                Parameter(
                    name="x", description="X", annotations=Annotation(display_name="X")
                )
            ],
            outputs=[
                Parameter(
                    name="result",
                    description="R",
                    annotations=Annotation(display_name="R"),
                ),
                Parameter(
                    name="count",
                    description="C",
                    annotations=Annotation(display_name="C"),
                ),
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(x: str) -> Tuple[str, int]:
            return (x, len(x))

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.output_schema)
        assert_data_validates(spec.output_schema, {"result": "test", "count": "4"})
        assert_data_fails_validation(spec.output_schema, {"result": "test"})
        self.assertEqual(spec.output_schema["output_order"], ["result", "count"])

    def test_function_with_default_parameters(self):
        @automation_activity(
            display_name="Defaults",
            description="Defaults",
            inputs=[
                Parameter(
                    name="x", description="X", annotations=Annotation(display_name="X")
                ),
                Parameter(
                    name="y", description="Y", annotations=Annotation(display_name="Y")
                ),
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(x: str, y: int = 42) -> str:
            return f"{x}_{y}"

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.input_schema)
        assert_data_validates(spec.input_schema, {"x": "test"})
        assert_data_validates(spec.input_schema, {"x": "test", "y": 100})
        assert_data_fails_validation(spec.input_schema, {"y": 100})

    def test_function_with_optional_parameters(self):
        @automation_activity(
            display_name="Optional",
            description="Optional",
            inputs=[
                Parameter(
                    name="x", description="X", annotations=Annotation(display_name="X")
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(x: Optional[str]) -> str:
            return x or ""

        spec = ACTIVITY_SPECS[0]
        x_prop = spec.input_schema["properties"]["x"]
        self.assertTrue(x_prop.get("nullable", False))

    def test_function_with_pydantic_model_return(self):
        @automation_activity(
            display_name="Model",
            description="Model",
            inputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            outputs=[
                Parameter(
                    name="output",
                    description="O",
                    annotations=Annotation(display_name="O"),
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(r: str) -> _TestOutputModel:
            return _TestOutputModel(result=r)

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.output_schema)
        assert_data_validates(spec.output_schema, {"output": {"result": "test"}})

    def test_function_with_list_input(self):
        @automation_activity(
            display_name="List",
            description="List",
            inputs=[
                Parameter(
                    name="paths",
                    description="P",
                    annotations=Annotation(display_name="P"),
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(paths: List[str]) -> str:
            return ",".join(paths)

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.input_schema)
        assert_data_validates(spec.input_schema, {"paths": ["a", "b"]})
        assert_data_fails_validation(spec.input_schema, {"paths": "not_array"})

    # -- schema_extra tests --

    def test_schema_extra_valid_keys_applied(self):
        @automation_activity(
            display_name="Extra",
            description="Extra",
            inputs=[
                Parameter(
                    name="items",
                    description="Items",
                    annotations=Annotation(display_name="Items"),
                    schema_extra={"minItems": 1, "maxItems": 10},
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(items: List[str]) -> str:
            return ",".join(items)

        spec = ACTIVITY_SPECS[0]
        items_prop = spec.input_schema["properties"]["items"]
        self.assertEqual(items_prop["minItems"], 1)
        self.assertEqual(items_prop["maxItems"], 10)

    def test_schema_extra_disallowed_key_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(
                        name="x",
                        description="X",
                        annotations=Annotation(display_name="X"),
                        schema_extra={"type": "string", "minLength": 1},
                    )
                ],
                outputs=[
                    Parameter(
                        name="r",
                        description="R",
                        annotations=Annotation(display_name="R"),
                    )
                ],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> str:
                return x

        self.assertIn("disallowed schema_extra keys", str(ctx.exception))
        self.assertIn("type", str(ctx.exception))

    # -- Annotation tests --

    def test_annotations_sub_type_in_input_schema(self):
        @automation_activity(
            display_name="SubType",
            description="SubType",
            inputs=[
                Parameter(
                    name="path",
                    description="Path",
                    annotations=Annotation(
                        sub_type=SubType.FILE_PATH, display_name="Path"
                    ),
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(path: str) -> str:
            return path

        spec = ACTIVITY_SPECS[0]
        path_prop = spec.input_schema["properties"]["path"]
        self.assertEqual(
            path_prop["x-automation-engine"]["sub_type"], SubType.FILE_PATH.value
        )

    def test_display_name_always_present(self):
        @automation_activity(
            display_name="DN",
            description="DN",
            inputs=[
                Parameter(
                    name="field",
                    description="Field",
                    annotations=Annotation(display_name="My Field"),
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(field: str) -> str:
            return field

        spec = ACTIVITY_SPECS[0]
        prop = spec.input_schema["properties"]["field"]
        self.assertEqual(prop["x-automation-engine"]["display_name"], "My Field")
        self.assertNotIn("sub_type", prop["x-automation-engine"])

    # -- Validation error tests --

    def test_input_count_mismatch_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(
                        name="x",
                        description="X",
                        annotations=Annotation(display_name="X"),
                    ),
                    Parameter(
                        name="y",
                        description="Y",
                        annotations=Annotation(display_name="Y"),
                    ),
                ],
                outputs=[
                    Parameter(
                        name="r",
                        description="R",
                        annotations=Annotation(display_name="R"),
                    )
                ],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> str:
                return x

        self.assertIn("doesn't match", str(ctx.exception))
        self.assertEqual(len(ACTIVITY_SPECS), 0)

    def test_input_name_mismatch_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(
                        name="wrong",
                        description="W",
                        annotations=Annotation(display_name="W"),
                    )
                ],
                outputs=[
                    Parameter(
                        name="r",
                        description="R",
                        annotations=Annotation(display_name="R"),
                    )
                ],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> str:
                return x

        self.assertIn("Missing in decorator", str(ctx.exception))
        self.assertEqual(len(ACTIVITY_SPECS), 0)

    def test_output_count_mismatch_raises_error(self):
        with self.assertRaises(ValueError):

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(
                        name="x",
                        description="X",
                        annotations=Annotation(display_name="X"),
                    )
                ],
                outputs=[
                    Parameter(
                        name="a",
                        description="A",
                        annotations=Annotation(display_name="A"),
                    ),
                    Parameter(
                        name="b",
                        description="B",
                        annotations=Annotation(display_name="B"),
                    ),
                ],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> str:
                return x

    def test_outputs_provided_no_return_annotation_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(
                        name="x",
                        description="X",
                        annotations=Annotation(display_name="X"),
                    )
                ],
                outputs=[
                    Parameter(
                        name="r",
                        description="R",
                        annotations=Annotation(display_name="R"),
                    )
                ],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str):
                return x

        self.assertIn("no return annotation", str(ctx.exception))

    def test_empty_inputs_with_params_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[],
                outputs=[],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> None:
                pass

        self.assertIn("inputs list is empty", str(ctx.exception).lower())

    def test_empty_outputs_with_return_type_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(
                        name="x",
                        description="X",
                        annotations=Annotation(display_name="X"),
                    )
                ],
                outputs=[],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> str:
                return x

        self.assertIn("outputs list is empty", str(ctx.exception).lower())

    # -- $defs hoisting tests --

    def test_extract_and_hoist_defs(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "$defs": {"TestEnum": {"enum": ["a", "b"], "type": "string"}},
                        "properties": {"field": {"$ref": "#/$defs/TestEnum"}},
                    },
                }
            },
        }
        collected: Dict[str, Any] = {}
        _extract_and_hoist_defs(schema, collected)
        self.assertIn("TestEnum", collected)
        self.assertNotIn("$defs", schema["properties"]["items"]["items"])

    def test_multiple_nested_defs_are_merged(self):
        schema = {
            "type": "object",
            "properties": {
                "f1": {"$defs": {"A": {"enum": ["a1"], "type": "string"}}},
                "f2": {"$defs": {"B": {"enum": ["b1"], "type": "string"}}},
            },
        }
        collected: Dict[str, Any] = {}
        _extract_and_hoist_defs(schema, collected)
        self.assertIn("A", collected)
        self.assertIn("B", collected)

    def test_list_of_pydantic_model_with_enum_hoists_defs(self):
        class _E(str, Enum):
            A = "a"
            B = "b"

        class _M(BaseModel):
            e: _E = Field(default=_E.A, description="Enum")
            n: str = Field(description="Name")

        @automation_activity(
            display_name="Hoist",
            description="Hoist",
            inputs=[
                Parameter(
                    name="items",
                    description="I",
                    annotations=Annotation(display_name="I"),
                )
            ],
            outputs=[
                Parameter(
                    name="r", description="R", annotations=Annotation(display_name="R")
                )
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(items: List[_M]) -> str:
            return str(len(items))

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.input_schema)
        self.assertIn("$defs", spec.input_schema)
        self.assertIn("_E", spec.input_schema["$defs"])

    # -- Metadata tests --

    def test_metadata_with_icon(self):
        @automation_activity(
            display_name="With Icon",
            description="With icon",
            inputs=[],
            outputs=[],
            category=ActivityCategory.DATA,
            metadata=ToolMetadata(icon="my-icon"),
        )
        def test_func() -> None:
            pass

        spec = ACTIVITY_SPECS[0]
        self.assertIsNotNone(spec.metadata)
        self.assertEqual(spec.metadata.icon, "my-icon")
        self.assertEqual(spec.category, ActivityCategory.DATA)

    # -- Category tests --

    def test_all_categories_accepted(self):
        for cat in ActivityCategory:

            @automation_activity(
                display_name=f"Cat {cat.value}",
                description=f"Cat {cat.value}",
                inputs=[],
                outputs=[],
                category=cat,
            )
            def test_func() -> None:
                pass

        self.assertEqual(len(ACTIVITY_SPECS), len(ActivityCategory))


# =============================================================================
# Resolution helper tests
# =============================================================================


class TestResolveHelpers(unittest.TestCase):
    def test_resolve_api_url_explicit(self):
        result = _resolve_automation_engine_api_url("http://my-host:9999")
        self.assertEqual(result, "http://my-host:9999")

    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_URL",
        "http://env-url:1234",
    )
    def test_resolve_api_url_from_env(self):
        result = _resolve_automation_engine_api_url(None)
        self.assertEqual(result, "http://env-url:1234")

    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_URL",
        None,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_HOST",
        "myhost",
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_PORT",
        "5555",
    )
    def test_resolve_api_url_from_host_port(self):
        result = _resolve_automation_engine_api_url(None)
        self.assertEqual(result, "http://myhost:5555")

    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_URL",
        None,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_HOST",
        None,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_PORT",
        None,
    )
    def test_resolve_api_url_none_when_nothing_set(self):
        result = _resolve_automation_engine_api_url(None)
        self.assertIsNone(result)

    def test_resolve_app_qualified_name_explicit(self):
        result = _resolve_app_qualified_name("custom/qn", "app")
        self.assertEqual(result, "custom/qn")

    @patch(
        "application_sdk.decorators.automation_activity.registration.APP_QUALIFIED_NAME",
        "env/qn",
    )
    def test_resolve_app_qualified_name_from_env(self):
        result = _resolve_app_qualified_name(None, "app")
        self.assertEqual(result, "env/qn")

    @patch(
        "application_sdk.decorators.automation_activity.registration.APP_QUALIFIED_NAME",
        None,
    )
    def test_resolve_app_qualified_name_computed(self):
        result = _resolve_app_qualified_name(None, "my-app")
        self.assertEqual(result, "default/apps/my_app")


# =============================================================================
# flush_activity_registrations tests
# =============================================================================


class TestFlushActivityRegistrations(unittest.TestCase):
    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    @pytest.mark.asyncio
    async def test_no_specs_logs_and_returns(self):
        """When ACTIVITY_SPECS is empty, flush should log and return early."""
        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:9999",
        )
        # No error, just returns

    @pytest.mark.asyncio
    async def test_no_url_warns_and_returns(self):
        """When no URL is configured, flush should warn and skip."""

        # Populate a spec
        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        with patch(
            "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_URL",
            None,
        ), patch(
            "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_HOST",
            None,
        ), patch(
            "application_sdk.decorators.automation_activity.registration.AUTOMATION_ENGINE_API_PORT",
            None,
        ):
            await flush_activity_registrations(
                app_name="test",
                workflow_task_queue="q",
                automation_engine_api_url=None,
            )

        # Specs should be restored (flush failed due to no URL)
        self.assertEqual(len(ACTIVITY_SPECS), 1)

    @pytest.mark.asyncio
    async def test_clears_global_specs_after_flush(self):
        """flush should consume and clear ACTIVITY_SPECS."""

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        # Even though the HTTP call will fail (no server), specs should be cleared
        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:99999",
            max_retries=0,
        )
        self.assertEqual(len(ACTIVITY_SPECS), 0)

    @pytest.mark.asyncio
    async def test_explicit_specs_does_not_clear_global(self):
        """When explicit activity_specs is passed, global ACTIVITY_SPECS should not be cleared."""

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:99999",
            activity_specs=[],  # explicit empty list
            max_retries=0,
        )
        # Global should NOT have been cleared
        self.assertEqual(len(ACTIVITY_SPECS), 1)


# =============================================================================
# URL validation tests
# =============================================================================


class TestValidateBaseUrl(unittest.TestCase):
    """Tests for _validate_base_url SSRF protection."""

    def test_http_url_accepted(self):
        _validate_base_url("http://localhost:8000")

    def test_https_url_accepted(self):
        _validate_base_url("https://engine.internal:443")

    def test_ftp_scheme_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_base_url("ftp://evil.com/data")
        self.assertIn("Unsupported URL scheme", str(ctx.exception))

    def test_file_scheme_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_base_url("file:///etc/passwd")
        self.assertIn("Unsupported URL scheme", str(ctx.exception))

    def test_no_hostname_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_base_url("http://")
        self.assertIn("no hostname", str(ctx.exception))

    def test_aws_metadata_endpoint_blocked(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_base_url("http://169.254.169.254/latest/meta-data/")
        self.assertIn("Blocked host", str(ctx.exception))

    def test_gcp_metadata_endpoint_blocked(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_base_url("http://metadata.google.internal/computeMetadata/v1/")
        self.assertIn("Blocked host", str(ctx.exception))

    @pytest.mark.asyncio
    async def test_flush_skips_on_invalid_url(self):
        """flush should log warning and skip when URL fails validation."""
        ACTIVITY_SPECS.clear()

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="ftp://evil.com",
            max_retries=0,
        )
        # Should not raise — graceful degradation
        ACTIVITY_SPECS.clear()


# =============================================================================
# isolated_activity_specs tests
# =============================================================================


class TestIsolatedActivitySpecs(unittest.TestCase):
    """Tests for the isolated_activity_specs context manager."""

    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    def test_context_manager_yields_empty_list(self):
        # Pre-populate some specs
        @automation_activity(
            display_name="Pre",
            description="Pre",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def pre_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        with isolated_activity_specs() as specs:
            # Inside the context, ACTIVITY_SPECS is empty
            self.assertEqual(len(specs), 0)
            self.assertEqual(len(ACTIVITY_SPECS), 0)

        # After the context, previous specs are restored
        self.assertEqual(len(ACTIVITY_SPECS), 1)
        self.assertEqual(ACTIVITY_SPECS[0].display_name, "Pre")

    def test_specs_registered_inside_context_are_discarded(self):
        with isolated_activity_specs() as specs:

            @automation_activity(
                display_name="Inside",
                description="Inside",
                inputs=[],
                outputs=[],
                category=ActivityCategory.UTILITY,
            )
            def inner_func() -> None:
                pass

            self.assertEqual(len(specs), 1)

        # After context, the inner spec is gone
        self.assertEqual(len(ACTIVITY_SPECS), 0)

    def test_restores_on_exception(self):
        @automation_activity(
            display_name="Safe",
            description="Safe",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def safe_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        with self.assertRaises(RuntimeError):
            with isolated_activity_specs():
                raise RuntimeError("boom")

        # Even after exception, original specs are restored
        self.assertEqual(len(ACTIVITY_SPECS), 1)
        self.assertEqual(ACTIVITY_SPECS[0].display_name, "Safe")


# =============================================================================
# Retry tests
# =============================================================================


class TestRequestWithRetry(unittest.TestCase):
    """Tests for _request_with_retry."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        result = await _request_with_retry(
            mock_client, "GET", "http://example.com", max_retries=2
        )
        self.assertEqual(result, mock_response)
        self.assertEqual(mock_client.request.call_count, 1)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
        new_callable=AsyncMock,
    )
    async def test_retries_on_connect_error(self, mock_sleep):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(
            side_effect=[
                httpx.ConnectError("Connection refused"),
                httpx.ConnectError("Connection refused"),
                mock_response,
            ]
        )

        result = await _request_with_retry(
            mock_client, "GET", "http://example.com", max_retries=2
        )
        self.assertEqual(result, mock_response)
        self.assertEqual(mock_client.request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
        new_callable=AsyncMock,
    )
    async def test_retries_on_retryable_status(self, mock_sleep):
        retry_response = MagicMock()
        retry_response.status_code = 503

        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=[retry_response, ok_response])

        result = await _request_with_retry(
            mock_client, "GET", "http://example.com", max_retries=2
        )
        self.assertEqual(result, ok_response)
        self.assertEqual(mock_client.request.call_count, 2)

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with patch(
            "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with self.assertRaises(httpx.ConnectError):
                await _request_with_retry(
                    mock_client, "GET", "http://example.com", max_retries=1
                )

        # 1 initial + 1 retry = 2 attempts
        self.assertEqual(mock_client.request.call_count, 2)

    @pytest.mark.asyncio
    async def test_non_retryable_status_raises_immediately(self):
        error_response = MagicMock()
        error_response.status_code = 404
        error_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found",
                request=MagicMock(),
                response=error_response,
            )
        )

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=error_response)

        with self.assertRaises(httpx.HTTPStatusError):
            await _request_with_retry(
                mock_client, "GET", "http://example.com", max_retries=3
            )

        # Should NOT retry on 404
        self.assertEqual(mock_client.request.call_count, 1)


# =============================================================================
# Short-circuit tests
# =============================================================================


class TestFlushShortCircuit(unittest.TestCase):
    """Tests that flush short-circuits when app upsert fails."""

    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration._request_with_retry",
        new_callable=AsyncMock,
    )
    async def test_upsert_failure_skips_tool_registration(self, mock_request):
        """When the app upsert fails, tool registration should be skipped."""
        health_response = MagicMock()
        health_response.status_code = 200

        mock_request.side_effect = [
            health_response,  # health check passes
            httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            ),  # upsert fails
        ]

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:8000",
            max_retries=0,
        )

        # Should only have called health check + upsert, NOT tool registration
        self.assertEqual(mock_request.call_count, 2)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration._request_with_retry",
        new_callable=AsyncMock,
    )
    async def test_successful_flow_calls_all_three(self, mock_request, mock_sleep):
        """On success, all three HTTP calls should be made."""
        ok_response = MagicMock()
        ok_response.status_code = 200
        mock_request.return_value = ok_response

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:8000",
            max_retries=0,
        )

        # health check + upsert + tools = 3 calls
        self.assertEqual(mock_request.call_count, 3)


# =============================================================================
# Contract tests — payload shape validation
# =============================================================================


class TestToolSpecContract(unittest.TestCase):
    """Validate the shape of payloads sent to the automation engine API."""

    def _make_activity_spec(self, **overrides):
        defaults = {
            "name": "my_activity",
            "display_name": "My Activity",
            "description": "Does things",
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "output_schema": {"type": "object", "properties": {}, "required": []},
            "category": ActivityCategory.UTILITY,
        }
        defaults.update(overrides)
        from application_sdk.decorators.automation_activity.models import ActivitySpec

        return ActivitySpec(**defaults)

    def test_tool_spec_has_required_fields(self):
        """ToolSpec built from ActivitySpec must contain all required API fields."""
        spec = self._make_activity_spec()
        tool = _build_tool_spec(spec)
        self.assertIsInstance(tool, ToolSpec)
        self.assertEqual(tool.name, "my_activity")
        self.assertEqual(tool.display_name, "My Activity")
        self.assertEqual(tool.category, "Utility")
        self.assertEqual(tool.description, "Does things")
        self.assertIsInstance(tool.input_schema, dict)
        self.assertIsInstance(tool.output_schema, dict)
        self.assertIsNone(tool.examples)
        self.assertIsNone(tool.metadata)

    def test_tool_spec_with_metadata_and_examples(self):
        """ToolSpec should carry metadata and examples when present."""
        spec = self._make_activity_spec(
            examples=["example1"],
            metadata=ToolMetadata(icon="star"),
        )
        tool = _build_tool_spec(spec)
        self.assertEqual(tool.examples, ["example1"])
        self.assertEqual(tool.metadata, {"icon": "star"})

    def test_tool_spec_serialisation_excludes_none(self):
        """Serialised ToolSpec should not include None-valued optional fields."""
        spec = self._make_activity_spec()
        tool = _build_tool_spec(spec)
        dumped = tool.model_dump(exclude_none=True)
        self.assertNotIn("examples", dumped)
        self.assertNotIn("metadata", dumped)
        # Required fields are present
        for key in (
            "name",
            "display_name",
            "category",
            "description",
            "input_schema",
            "output_schema",
        ):
            self.assertIn(key, dumped)

    def test_tool_registration_request_shape(self):
        """ToolRegistrationRequest should serialise to the expected API shape."""
        spec = self._make_activity_spec()
        tool = _build_tool_spec(spec)
        request = ToolRegistrationRequest(
            app_qualified_name="default/apps/my_app",
            app_name="my_app",
            task_queue="q",
            tools=[tool],
        )
        dumped = request.model_dump(exclude_none=True)
        self.assertEqual(dumped["app_qualified_name"], "default/apps/my_app")
        self.assertEqual(dumped["app_name"], "my_app")
        self.assertEqual(dumped["task_queue"], "q")
        self.assertEqual(len(dumped["tools"]), 1)
        self.assertEqual(dumped["tools"][0]["name"], "my_activity")

    def test_tool_registration_request_rejects_empty_tools(self):
        """ToolRegistrationRequest with empty tools should still be valid (API decides)."""
        request = ToolRegistrationRequest(
            app_qualified_name="default/apps/x",
            app_name="x",
            task_queue="q",
            tools=[],
        )
        self.assertEqual(len(request.tools), 0)


# =============================================================================
# Output schema_extra tests
# =============================================================================


class TestOutputSchemaExtra(unittest.TestCase):
    """Tests for schema_extra on output parameters."""

    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    def test_output_schema_extra_valid_keys_applied(self):
        """Valid schema_extra on an output parameter should be merged into the schema."""

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[
                Parameter(
                    name="result",
                    description="Result",
                    annotations=Annotation(display_name="Result"),
                    schema_extra={"minLength": 1},
                ),
            ],
            category=ActivityCategory.UTILITY,
        )
        def my_func() -> str:
            return "ok"

        spec = ACTIVITY_SPECS[-1]
        result_schema = spec.output_schema["properties"]["result"]
        self.assertEqual(result_schema["minLength"], 1)

    def test_output_schema_extra_disallowed_key_raises(self):
        """Disallowed schema_extra key on output parameter should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="T",
                description="T",
                inputs=[],
                outputs=[
                    Parameter(
                        name="result",
                        description="Result",
                        annotations=Annotation(display_name="Result"),
                        schema_extra={"type": "number"},
                    ),
                ],
                category=ActivityCategory.UTILITY,
            )
            def my_func() -> str:
                return "ok"

        self.assertIn("disallowed schema_extra keys", str(ctx.exception))

    def test_output_schema_extra_on_tuple_return(self):
        """schema_extra should work on tuple-return output parameters."""

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[
                Parameter(
                    name="a",
                    description="A",
                    annotations=Annotation(display_name="A"),
                    schema_extra={"maxLength": 100},
                ),
                Parameter(
                    name="b",
                    description="B",
                    annotations=Annotation(display_name="B"),
                ),
            ],
            category=ActivityCategory.UTILITY,
        )
        def my_func() -> Tuple[str, str]:
            return ("x", "y")

        spec = ACTIVITY_SPECS[-1]
        self.assertEqual(spec.output_schema["properties"]["a"]["maxLength"], 100)
        # b should NOT have maxLength
        self.assertNotIn("maxLength", spec.output_schema["properties"]["b"])


# =============================================================================
# Specs preservation on flush failure
# =============================================================================


class TestSpecsPreservationOnFailure(unittest.TestCase):
    """Tests that specs are re-added to ACTIVITY_SPECS when flush fails."""

    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration._request_with_retry",
        new_callable=AsyncMock,
    )
    async def test_specs_restored_on_tool_registration_failure(self, mock_request):
        """When tool registration fails, specs should be put back in ACTIVITY_SPECS."""
        health_ok = MagicMock()
        health_ok.status_code = 200

        mock_request.side_effect = [
            health_ok,  # health check
            health_ok,  # upsert
            httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            ),  # tool registration fails
        ]

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        with patch(
            "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await flush_activity_registrations(
                app_name="test",
                workflow_task_queue="q",
                automation_engine_api_url="http://localhost:8000",
                max_retries=0,
            )

        # Specs should be restored after failure
        self.assertEqual(len(ACTIVITY_SPECS), 1)
        self.assertEqual(ACTIVITY_SPECS[0].name, "test_func")

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration._request_with_retry",
        new_callable=AsyncMock,
    )
    async def test_specs_cleared_on_success(self, mock_request, mock_sleep):
        """When flush succeeds, specs should NOT be restored."""
        ok_response = MagicMock()
        ok_response.status_code = 200
        mock_request.return_value = ok_response

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:8000",
            max_retries=0,
        )

        # Specs should be consumed and NOT restored
        self.assertEqual(len(ACTIVITY_SPECS), 0)

    @pytest.mark.asyncio
    async def test_specs_not_restored_when_explicit_list_provided(self):
        """When activity_specs is explicitly passed, global ACTIVITY_SPECS is untouched."""
        from application_sdk.decorators.automation_activity.models import ActivitySpec

        explicit = [
            ActivitySpec(
                name="x",
                display_name="X",
                description="X",
                input_schema={},
                output_schema={},
                category=ActivityCategory.UTILITY,
            )
        ]

        # Put something in the global list to verify it's not touched
        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        self.assertEqual(len(ACTIVITY_SPECS), 1)

        with patch(
            "application_sdk.decorators.automation_activity.registration._flush_specs",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await flush_activity_registrations(
                app_name="test",
                workflow_task_queue="q",
                automation_engine_api_url="http://localhost:8000",
                activity_specs=explicit,
            )

        # Global ACTIVITY_SPECS should be untouched (not cleared, not extended)
        self.assertEqual(len(ACTIVITY_SPECS), 1)
        self.assertEqual(ACTIVITY_SPECS[0].name, "test_func")


# =============================================================================
# Configurable propagation delay
# =============================================================================


class TestConfigurablePropagationDelay(unittest.TestCase):
    """Tests that the propagation delay is configurable via env var."""

    def setUp(self):
        ACTIVITY_SPECS.clear()

    def tearDown(self):
        ACTIVITY_SPECS.clear()

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration._request_with_retry",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.APP_UPSERT_PROPAGATION_DELAY",
        "2.5",
    )
    async def test_custom_delay_from_env(self, mock_sleep, mock_request):
        """Propagation delay should be read from env var."""
        ok_response = MagicMock()
        ok_response.status_code = 200
        mock_request.return_value = ok_response

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:8000",
            max_retries=0,
        )

        mock_sleep.assert_called_once_with(2.5)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.decorators.automation_activity.registration._request_with_retry",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.decorators.automation_activity.registration.asyncio.sleep",
        new_callable=AsyncMock,
    )
    async def test_default_delay_without_env(self, mock_sleep, mock_request):
        """Without env var, default delay (5.0) should be used."""
        ok_response = MagicMock()
        ok_response.status_code = 200
        mock_request.return_value = ok_response

        @automation_activity(
            display_name="T",
            description="T",
            inputs=[],
            outputs=[],
            category=ActivityCategory.UTILITY,
        )
        def test_func() -> None:
            pass

        await flush_activity_registrations(
            app_name="test",
            workflow_task_queue="q",
            automation_engine_api_url="http://localhost:8000",
            max_retries=0,
        )

        mock_sleep.assert_called_once_with(5.0)

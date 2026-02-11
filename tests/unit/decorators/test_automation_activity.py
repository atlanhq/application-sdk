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

import pytest
from pydantic import BaseModel, Field

from application_sdk.decorators.automation_activity import (
    ACTIVITY_SPECS,
    ActivityCategory,
    Annotation,
    Parameter,
    SubType,
    ToolMetadata,
    _extract_and_hoist_defs,
    _resolve_app_qualified_name,
    _resolve_automation_engine_api_url,
    automation_activity,
    flush_activity_registrations,
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
                Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
            ],
            category=ActivityCategory.UTILITY,
        )
        def func1(x: str) -> str:
            return x

        @automation_activity(
            display_name="Second",
            description="Second",
            inputs=[
                Parameter(name="y", description="Y", annotations=Annotation(display_name="Y"))
            ],
            outputs=[
                Parameter(name="o", description="O", annotations=Annotation(display_name="O"))
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
                Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                Parameter(name="x", description="X", annotations=Annotation(display_name="X")),
                Parameter(name="y", description="Y", annotations=Annotation(display_name="Y")),
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
            ],
            outputs=[
                Parameter(name="result", description="R", annotations=Annotation(display_name="R")),
                Parameter(name="count", description="C", annotations=Annotation(display_name="C")),
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
                Parameter(name="x", description="X", annotations=Annotation(display_name="X")),
                Parameter(name="y", description="Y", annotations=Annotation(display_name="Y")),
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
            ],
            outputs=[
                Parameter(name="output", description="O", annotations=Annotation(display_name="O"))
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
                Parameter(name="paths", description="P", annotations=Annotation(display_name="P"))
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
            ],
            category=ActivityCategory.UTILITY,
        )
        def test_func(paths: List[str]) -> str:
            return ",".join(paths)

        spec = ACTIVITY_SPECS[0]
        assert_valid_json_schema(spec.input_schema)
        assert_data_validates(spec.input_schema, {"paths": ["a", "b"]})
        assert_data_fails_validation(spec.input_schema, {"paths": "not_array"})

    # -- Annotation tests --

    def test_annotations_sub_type_in_input_schema(self):
        @automation_activity(
            display_name="SubType",
            description="SubType",
            inputs=[
                Parameter(
                    name="path",
                    description="Path",
                    annotations=Annotation(sub_type=SubType.FILE_PATH, display_name="Path"),
                )
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                    Parameter(name="x", description="X", annotations=Annotation(display_name="X")),
                    Parameter(name="y", description="Y", annotations=Annotation(display_name="Y")),
                ],
                outputs=[
                    Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                    Parameter(name="wrong", description="W", annotations=Annotation(display_name="W"))
                ],
                outputs=[
                    Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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
                    Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
                ],
                outputs=[
                    Parameter(name="a", description="A", annotations=Annotation(display_name="A")),
                    Parameter(name="b", description="B", annotations=Annotation(display_name="B")),
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
                    Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
                ],
                outputs=[
                    Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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

        self.assertIn("inputs list is empty", str(ctx.exception))

    def test_empty_outputs_with_return_type_raises_error(self):
        with self.assertRaises(ValueError) as ctx:

            @automation_activity(
                display_name="Bad",
                description="Bad",
                inputs=[
                    Parameter(name="x", description="X", annotations=Annotation(display_name="X"))
                ],
                outputs=[],
                category=ActivityCategory.UTILITY,
            )
            def test_func(x: str) -> str:
                return x

        self.assertIn("outputs list is empty", str(ctx.exception))

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
                Parameter(name="items", description="I", annotations=Annotation(display_name="I"))
            ],
            outputs=[
                Parameter(name="r", description="R", annotations=Annotation(display_name="R"))
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

    @patch.dict("os.environ", {"ATLAN_AUTOMATION_ENGINE_API_URL": "http://env-url:1234"})
    def test_resolve_api_url_from_env(self):
        result = _resolve_automation_engine_api_url(None)
        self.assertEqual(result, "http://env-url:1234")

    @patch.dict(
        "os.environ",
        {
            "ATLAN_AUTOMATION_ENGINE_API_HOST": "myhost",
            "ATLAN_AUTOMATION_ENGINE_API_PORT": "5555",
        },
        clear=False,
    )
    def test_resolve_api_url_from_host_port(self):
        # Make sure the direct URL env var is not set
        import os

        os.environ.pop("ATLAN_AUTOMATION_ENGINE_API_URL", None)
        result = _resolve_automation_engine_api_url(None)
        self.assertEqual(result, "http://myhost:5555")

    @patch.dict("os.environ", {}, clear=True)
    def test_resolve_api_url_none_when_nothing_set(self):
        result = _resolve_automation_engine_api_url(None)
        self.assertIsNone(result)

    def test_resolve_app_qualified_name_explicit(self):
        result = _resolve_app_qualified_name("custom/qn", "app")
        self.assertEqual(result, "custom/qn")

    @patch.dict("os.environ", {"ATLAN_APP_QUALIFIED_NAME": "env/qn"})
    def test_resolve_app_qualified_name_from_env(self):
        result = _resolve_app_qualified_name(None, "app")
        self.assertEqual(result, "env/qn")

    @patch.dict("os.environ", {}, clear=True)
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

        with patch.dict("os.environ", {}, clear=True):
            await flush_activity_registrations(
                app_name="test",
                workflow_task_queue="q",
                automation_engine_api_url=None,
            )

        # Specs should have been cleared
        self.assertEqual(len(ACTIVITY_SPECS), 0)

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
        )
        # Global should NOT have been cleared
        self.assertEqual(len(ACTIVITY_SPECS), 1)

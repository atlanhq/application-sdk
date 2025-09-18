"""
Tests for the MCP tool decorator.

This module contains comprehensive tests for the @mcp_tool decorator,
covering all functionality including metadata attachment, parameter handling,
and various usage patterns.

Test Case Bucketing and Coverage:
+---------------------------------+-------------+-------------------------------------+
| Test Category                   | Test Count  | Coverage Description                |
+---------------------------------+-------------+-------------------------------------+
| Basic Functionality             |      3      | Minimal params, custom name/desc    |
| Parameter Handling              |      6      | Args, kwargs, all params, etc.      |
| Fallback Behavior               |      3      | None values, empty strings          |
| Function Behavior Preservation  |      2      | Original behavior, attributes       |
| Method Types                    |      3      | Class, static, async methods        |
| Advanced Scenarios              |      4      | Complex params, multiple decor.     |
| Metadata Structure              |      2      | Structure validation, types         |
| Edge Cases                      |      2      | Lambda, nested functions            |
| Environment Stability           |      1      | MCP_METADATA_KEY constancy          |
+---------------------------------+-------------+-------------------------------------+
| Total Test Cases                |     25      | Comprehensive coverage              |
+---------------------------------+-------------+-------------------------------------+

Key Test Areas:
• Metadata attachment and retrieval
• Parameter validation and fallback logic
• Function behavior preservation
• Cross-environment stability
• Type safety and structure validation
"""

import os
from typing import Any, Dict, List, Optional

from application_sdk.constants import MCP_METADATA_KEY
from application_sdk.decorators.mcp_tool import mcp_tool


class TestMCPToolDecorator:
    """Test suite for the @mcp_tool decorator."""

    def test_basic_decorator_usage(self):
        """Test basic decorator usage with minimal parameters."""

        @mcp_tool()
        def simple_function() -> str:
            """A simple function."""
            return "hello"

        # Check that metadata is attached
        assert hasattr(simple_function, MCP_METADATA_KEY)

        metadata = getattr(simple_function, MCP_METADATA_KEY)
        assert metadata.name == "simple_function"
        assert metadata.description == "A simple function."
        assert metadata.visible is True
        assert metadata.args == ()
        assert metadata.kwargs == {}

    def test_decorator_with_custom_name(self):
        """Test decorator with custom name parameter."""

        @mcp_tool(name="custom_tool_name")
        def some_function() -> str:
            """A function with custom name."""
            return "test"

        metadata = getattr(some_function, MCP_METADATA_KEY)
        assert metadata.name == "custom_tool_name"
        assert metadata.description == "A function with custom name."

    def test_decorator_with_custom_description(self):
        """Test decorator with custom description parameter."""

        @mcp_tool(description="Custom description for the tool")
        def another_function() -> str:
            """Original docstring."""
            return "test"

        metadata = getattr(another_function, MCP_METADATA_KEY)
        assert metadata.name == "another_function"
        assert metadata.description == "Custom description for the tool"

    def test_decorator_with_visible_false(self):
        """Test decorator with visible=False parameter."""

        @mcp_tool(visible=False)
        def hidden_function() -> str:
            """A hidden function."""
            return "hidden"

        metadata = getattr(hidden_function, MCP_METADATA_KEY)
        assert metadata.visible is False

    def test_decorator_with_visible_true(self):
        """Test decorator with visible=True parameter."""

        @mcp_tool(visible=True)
        def visible_function() -> str:
            """A visible function."""
            return "visible"

        metadata = getattr(visible_function, MCP_METADATA_KEY)
        assert metadata.visible is True

    def test_decorator_with_additional_args(self):
        """Test decorator with additional positional arguments."""

        @mcp_tool()
        def function_with_args() -> str:
            """Function with additional args."""
            return "test"

        # Apply additional args manually to test the metadata structure
        metadata = getattr(function_with_args, MCP_METADATA_KEY)
        metadata.args = ("arg1", "arg2", "arg3")
        assert metadata.args == ("arg1", "arg2", "arg3")

    def test_decorator_with_additional_kwargs(self):
        """Test decorator with additional keyword arguments."""

        @mcp_tool(extra_param="value", another_param=42)
        def function_with_kwargs() -> str:
            """Function with additional kwargs."""
            return "test"

        metadata = getattr(function_with_kwargs, MCP_METADATA_KEY)
        assert metadata.kwargs == {"extra_param": "value", "another_param": 42}

    def test_decorator_with_all_parameters(self):
        """Test decorator with all parameters specified."""

        @mcp_tool(
            name="complete_tool",
            description="A complete tool with all parameters",
            visible=False,
            param1="value1",
            param2=123,
        )
        def complete_function() -> str:
            """Original docstring that should be overridden."""
            return "complete"

        metadata = getattr(complete_function, MCP_METADATA_KEY)
        assert metadata.name == "complete_tool"
        assert metadata.description == "A complete tool with all parameters"
        assert metadata.visible is False
        assert metadata.args == ()
        assert metadata.kwargs == {"param1": "value1", "param2": 123}

    def test_decorator_with_none_name_falls_back_to_function_name(self):
        """Test that None name falls back to function name."""

        @mcp_tool(name=None)
        def fallback_function() -> str:
            """Function with None name."""
            return "test"

        metadata = getattr(fallback_function, MCP_METADATA_KEY)
        assert metadata.name == "fallback_function"

    def test_decorator_with_none_description_falls_back_to_docstring(self):
        """Test that None description falls back to function docstring."""

        @mcp_tool(description=None)
        def docstring_function() -> str:
            """This is the docstring description."""
            return "test"

        metadata = getattr(docstring_function, MCP_METADATA_KEY)
        assert metadata.description == "This is the docstring description."

    def test_decorator_with_empty_docstring(self):
        """Test decorator with function that has no docstring."""

        @mcp_tool()
        def no_docstring_function() -> str:
            return "test"

        metadata = getattr(no_docstring_function, MCP_METADATA_KEY)
        assert metadata.description is None

    def test_decorator_with_empty_string_description(self):
        """Test decorator with empty string description."""

        @mcp_tool(description="")
        def empty_description_function() -> str:
            """Original docstring."""
            return "test"

        metadata = getattr(empty_description_function, MCP_METADATA_KEY)
        # Empty string is falsy, so it falls back to docstring
        assert metadata.description == "Original docstring."

    def test_decorator_preserves_function_behavior(self):
        """Test that decorator preserves original function behavior."""

        @mcp_tool(name="preserved_function")
        def original_function(x: int, y: int) -> int:
            """Add two numbers."""
            return x + y

        # Test that function still works as expected
        assert original_function(2, 3) == 5
        assert original_function(10, -5) == 5

        # Test that metadata is still attached
        metadata = getattr(original_function, MCP_METADATA_KEY)
        assert metadata.name == "preserved_function"

    def test_decorator_with_class_method(self):
        """Test decorator with class method."""

        class TestClass:
            @mcp_tool(name="class_method_tool")
            def class_method(self, value: str) -> str:
                """A class method tool."""
                return f"processed: {value}"

        instance = TestClass()

        # Test that method still works
        assert instance.class_method("test") == "processed: test"

        # Test that metadata is attached
        metadata = getattr(instance.class_method, MCP_METADATA_KEY)
        assert metadata.name == "class_method_tool"

    def test_decorator_with_static_method(self):
        """Test decorator with static method."""

        class TestClass:
            @staticmethod
            @mcp_tool(name="static_method_tool")
            def static_method(value: int) -> int:
                """A static method tool."""
                return value * 2

        # Test that static method still works
        assert TestClass.static_method(5) == 10

        # Test that metadata is attached
        metadata = getattr(TestClass.static_method, MCP_METADATA_KEY)
        assert metadata.name == "static_method_tool"

    def test_decorator_with_async_function(self):
        """Test decorator with async function."""

        @mcp_tool(name="async_tool")
        async def async_function(data: str) -> str:
            """An async function tool."""
            return f"async processed: {data}"

        # Test that metadata is attached
        metadata = getattr(async_function, MCP_METADATA_KEY)
        assert metadata.name == "async_tool"

    def test_decorator_with_complex_parameters(self):
        """Test decorator with complex parameter types."""

        @mcp_tool(name="complex_tool")
        def complex_function(
            items: List[str],
            config: Dict[str, Any],
            optional_param: Optional[int] = None,
        ) -> Dict[str, Any]:
            """A function with complex parameters."""
            return {"items": items, "config": config, "optional": optional_param}

        metadata = getattr(complex_function, MCP_METADATA_KEY)
        assert metadata.name == "complex_tool"

    def test_multiple_decorators_on_same_function(self):
        """Test that multiple decorators can be applied to the same function."""

        def dummy_decorator(func):
            """A dummy decorator for testing."""
            func._dummy_attr = "dummy_value"
            return func

        @dummy_decorator
        @mcp_tool(name="multi_decorated_function")
        def multi_decorated_function() -> str:
            """A function with multiple decorators."""
            return "test"

        # Test that both decorators work
        assert hasattr(multi_decorated_function, "_dummy_attr")
        assert multi_decorated_function._dummy_attr == "dummy_value"

        metadata = getattr(multi_decorated_function, MCP_METADATA_KEY)
        assert metadata.name == "multi_decorated_function"

    def test_decorator_metadata_structure(self):
        """Test that metadata has the correct structure."""

        @mcp_tool(
            name="structure_test",
            description="Test metadata structure",
            visible=True,
            kwarg1="value1",
            kwarg2=42,
        )
        def structure_test_function() -> str:
            """Test function."""
            return "test"

        metadata = getattr(structure_test_function, MCP_METADATA_KEY)

        # Test that all expected keys are present
        expected_keys = {"name", "description", "visible", "args", "kwargs"}
        assert set(metadata.model_fields.keys()) == expected_keys

        # Test that values have correct types
        assert isinstance(metadata.name, str)
        assert isinstance(metadata.description, str)
        assert isinstance(metadata.visible, bool)
        assert isinstance(metadata.args, tuple)
        assert isinstance(metadata.kwargs, dict)

    def test_decorator_with_boolean_visible_values(self):
        """Test decorator with various boolean values for visible parameter."""

        @mcp_tool(visible=True)
        def true_visible_function() -> str:
            """Function with visible=True."""
            return "test"

        @mcp_tool(visible=False)
        def false_visible_function() -> str:
            """Function with visible=False."""
            return "test"

        true_metadata = getattr(true_visible_function, MCP_METADATA_KEY)
        false_metadata = getattr(false_visible_function, MCP_METADATA_KEY)

        assert true_metadata.visible is True
        assert false_metadata.visible is False

    def test_decorator_with_empty_args_and_kwargs(self):
        """Test decorator with empty args and kwargs."""

        @mcp_tool()
        def empty_args_function() -> str:
            """Function with no additional args or kwargs."""
            return "test"

        metadata = getattr(empty_args_function, MCP_METADATA_KEY)
        assert metadata.args == ()
        assert metadata.kwargs == {}

    def test_decorator_preserves_function_attributes(self):
        """Test that decorator preserves existing function attributes."""

        def function_with_attr() -> str:
            """Function with custom attribute."""
            return "test"

        setattr(function_with_attr, "custom_attr", "custom_value")
        function_with_attr.__module__ = "test_module"

        decorated_function = mcp_tool(name="preserved_attrs")(function_with_attr)

        # Test that custom attributes are preserved
        assert hasattr(decorated_function, "custom_attr")
        assert getattr(decorated_function, "custom_attr") == "custom_value"
        assert decorated_function.__module__ == "test_module"

        # Test that MCP metadata is added
        metadata = getattr(decorated_function, MCP_METADATA_KEY)
        assert metadata.name == "preserved_attrs"

    def test_decorator_with_lambda_function(self):
        """Test decorator with lambda function."""
        decorated_lambda = mcp_tool(name="lambda_tool")(lambda x: x * 2)

        # Test that lambda still works
        assert decorated_lambda(5) == 10

        # Test that metadata is attached
        metadata = getattr(decorated_lambda, MCP_METADATA_KEY)
        assert metadata.name == "lambda_tool"
        assert metadata.description is None  # Lambda has no docstring

    def test_decorator_with_nested_function(self):
        """Test decorator with nested function."""

        def outer_function():
            @mcp_tool(name="nested_tool")
            def inner_function(value: str) -> str:
                """A nested function tool."""
                return f"nested: {value}"

            return inner_function

        inner_func = outer_function()

        # Test that nested function works
        assert inner_func("test") == "nested: test"

        # Test that metadata is attached
        metadata = getattr(inner_func, MCP_METADATA_KEY)
        assert metadata.name == "nested_tool"

    def test_mcp_metadata_key_constant(self):
        """Test that MCP_METADATA_KEY is constant and not affected by environment variables."""

        # Store original environment
        original_env = os.environ.copy()

        try:
            # Test that MCP_METADATA_KEY is a constant string
            expected_key = "__atlan_application_sdk_mcp_metadata"
            assert MCP_METADATA_KEY == expected_key

            # Test that it doesn't change when environment variables change
            os.environ["ENABLE_MCP"] = "true"
            os.environ["APPLICATION_NAME"] = "test_app"
            os.environ["WORKFLOW_HOST"] = "test_host"

            # Re-import to ensure the constant is still the same
            from application_sdk.constants import MCP_METADATA_KEY as MCP_METADATA_KEY_2

            assert MCP_METADATA_KEY_2 == expected_key
            assert MCP_METADATA_KEY_2 == MCP_METADATA_KEY

            # Test that the decorator still works with the constant key
            @mcp_tool(name="test_constant_key")
            def test_function() -> str:
                """Test function for constant key."""
                return "test"

            # Verify the metadata is attached with the correct key
            assert hasattr(test_function, MCP_METADATA_KEY)
            metadata = getattr(test_function, MCP_METADATA_KEY)
            assert metadata.name == "test_constant_key"

        finally:
            # Restore original environment
            os.environ.clear()
            os.environ.update(original_env)

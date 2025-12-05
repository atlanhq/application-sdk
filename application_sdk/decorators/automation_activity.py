"""
Activity registration decorator for automatic registration with the automation engine.
"""

import inspect
import json
from typing import Any, Callable, Dict, List, Optional, Union, get_args, get_origin

from pydantic import BaseModel
import requests

from application_sdk.constants import AUTOMATION_ENGINE_API_URL
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Global registry to collect decorated activities
ACTIVITY_SPECS: List[dict[str, Any]] = []


def _type_to_json_schema(annotation: Any) -> Dict[str, Any]:
    """Convert Python type annotation to JSON Schema using reflection."""
    # Pydantic models
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        schema = annotation.model_json_schema(mode="serialization")
        if "$defs" in schema:
            schema = {**schema, **schema.pop("$defs")}
        return schema

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional types (Union with None)
    if origin is Union and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            schema = _type_to_json_schema(non_none[0])
            schema["nullable"] = True
            return schema

    # Dict types
    if origin is dict or annotation is dict:
        return {
            "type": "object",
            "additionalProperties": _type_to_json_schema(args[1]) if args else {},
        }

    # List types
    if origin is list or annotation is list:
        return {"type": "array", "items": _type_to_json_schema(args[0]) if args else {}}

    # Tuple types
    if origin is tuple or annotation is tuple:
        if args:
            item_schemas = [_type_to_json_schema(arg) for arg in args]
            return {
                "type": "array",
                "items": item_schemas,
                "minItems": len(item_schemas),
                "maxItems": len(item_schemas),
            }
        return {"type": "array"}

    # Primitive types
    type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if annotation in type_map:
        return {"type": type_map[annotation]}

    return {}


def _create_object_schema(
    properties: Dict[str, Any], required: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Create a JSON Schema object with properties and required fields."""
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _generate_input_schema(func: Any) -> Dict[str, Any]:
    """Generate JSON Schema for function inputs using reflection."""
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, param in inspect.signature(func).parameters.items():
        if name == "self":
            continue
        schema = (
            _type_to_json_schema(param.annotation)
            if param.annotation != inspect.Parameter.empty
            else {}
        )
        if param.default != inspect.Parameter.empty:
            if isinstance(param.default, (str, int, float, bool, type(None))):
                schema["default"] = param.default
        else:
            required.append(name)
        properties[name] = schema
    return _create_object_schema(properties, required if required else None)


def _validate_output_field_names(
    func: Any, output_field_names: List[str], return_annotation: Any
) -> None:
    """Validate output_field_names against function return type."""
    origin = get_origin(return_annotation)
    is_tuple: bool = origin is tuple or return_annotation is tuple

    if is_tuple:
        args = get_args(return_annotation)
        if not args:
            raise ValueError(
                f"output_field_names provided for '{func.__name__}', but tuple has no type arguments."
            )
        if len(output_field_names) != len(args):
            raise ValueError(
                f"output_field_names length ({len(output_field_names)}) doesn't match "
                f"tuple length ({len(args)}) for '{func.__name__}'. Expected {len(args)} field names."
            )
    else:
        # For single return types (Pydantic models, primitives, etc.), require exactly 1 field name
        if len(output_field_names) != 1:
            raise ValueError(
                f"output_field_names provided for '{func.__name__}' with single return type, "
                f"but length ({len(output_field_names)}) is not 1. "
                "For single return types, provide exactly one field name."
            )


def _generate_output_schema(
    func: Any, output_field_names: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Generate JSON Schema for function outputs using reflection."""
    return_annotation = inspect.signature(func).return_annotation
    has_return = return_annotation != inspect.Signature.empty

    if output_field_names:
        if not has_return:
            raise ValueError(
                f"output_field_names provided for '{func.__name__}', but function has no return annotation."
            )
        _validate_output_field_names(func, output_field_names, return_annotation)

    if not has_return:
        return {}

    schema: dict[str, Any] = _type_to_json_schema(return_annotation)

    # Wrap in object with named properties if output_field_names provided
    if output_field_names:
        # Handle tuple conversion to object
        if schema.get("type") == "array" and "items" in schema:
            items = schema.get("items", [])
            if isinstance(items, list) and len(items) == len(output_field_names):
                properties = {
                    name: item for name, item in zip(output_field_names, items)
                }
                return _create_object_schema(properties, output_field_names)
        # Handle single return types (Pydantic models, primitives, etc.) - wrap in object
        elif len(output_field_names) == 1:
            properties = {output_field_names[0]: schema}
            return _create_object_schema(properties, output_field_names)

    return schema


def automation_activity(
    description: str,
    output_field_names: Optional[List[str]] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to mark an activity for automatic registration.

    Args:
        description: Description of what the activity does.
        output_field_names: Optional list of field names for return types.
            - For tuple return types: Length must match the number of tuple elements.
              Converts tuple outputs to objects with named properties.
            - For single return types (Pydantic models, primitives like str/int, etc.):
              Must have exactly one field name. Wraps the return value in an object
              with the given field name.

    Returns:
        Decorator function.

    Raises:
        ValueError: If output_field_names is provided but function has no return annotation,
            or if lengths don't match (tuple length for tuples, exactly 1 for single types).

    Example:
        >>> # Tuple return type
        >>> @automation_activity(
        ...     description="Fetch entities by DSL",
        ...     output_field_names=["entities_path", "total_count", "chunk_count"]
        ... )
        ... def fetch_entities(self, dsl_query: dict) -> tuple[str, int, int]:
        ...     return ("path/to/entities", 1000, 10)
        ...
        >>> # Pydantic model return type
        >>> @automation_activity(
        ...     description="Update metadata",
        ...     output_field_names=["update_metadata_output"]
        ... )
        ... def update_metadata(self, ...) -> UpdateMetadataOutput:
        ...     return UpdateMetadataOutput(...)
        ...
        >>> # Primitive return type
        >>> @automation_activity(
        ...     description="Get count",
        ...     output_field_names=["count"]
        ... )
        ... def get_count(self) -> int:
        ...     return 42
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        name: str = func.__name__
        input_schema: dict[str, Any] = _generate_input_schema(func)
        output_schema: dict[str, Any] = _generate_output_schema(
            func, output_field_names
        )

        logger.info(f"Collected automation activity: {name}")
        ACTIVITY_SPECS.append(
            {
                "name": name,
                "description": description,
                "func": func,
                "input_schema": json.dumps(input_schema) if input_schema else None,
                "output_schema": json.dumps(output_schema) if output_schema else None,
            }
        )
        return func

    return decorator


def flush_activity_registrations(
    app_name: str,
    workflow_task_queue: str,
    activity_specs: List[dict[str, Any]],
) -> None:
    """Flush all collected registrations by calling the activities create API via HTTP."""
    if not activity_specs:
        logger.info("No activities to register")
        return

    if not AUTOMATION_ENGINE_API_URL:
        logger.warning(
            "Automation engine API URL not configured. Skipping activity registration."
        )
        return

    # Perform health check first
    try:
        health_check_url: str = f"{AUTOMATION_ENGINE_API_URL}/api/health"
        health_response: requests.Response = requests.get(health_check_url, timeout=5.0)
        health_response.raise_for_status()
        logger.info("Automation engine health check passed")
    except Exception as e:
        logger.warning(
            f"Automation engine health check failed: {e}. "
            "Skipping activity registration. "
            "Check if the automation engine is deployed and accessible."
        )
        return

    logger.info(f"Registering {len(ACTIVITY_SPECS)} activities with automation engine")

    # Generate app qualified name
    app_qualified_name: str = f"default/apps/{app_name}"

    # Build tools payload without function objects (not JSON serializable)
    tools = [
        {
            "name": item["name"],
            "description": item["description"],
            "input_schema": item["input_schema"],
            "output_schema": item["output_schema"],
        }
        for item in activity_specs
    ]

    payload = {
        "app_qualified_name": app_qualified_name,
        "app_name": app_name,
        "task_queue": workflow_task_queue,
        "tools": tools,
    }

    try:
        response: requests.Response = requests.post(
            f"{AUTOMATION_ENGINE_API_URL}/api/tools",
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()

        if result.get("status") == "success":
            logger.info(
                f"Successfully registered {len(tools)} activities with automation engine"
            )
        else:
            logger.warning(
                f"Failed to register activities with automation engine: {result.get('message')}"
            )
    except Exception as e:
        raise Exception(
            f"Failed to register activities with automation engine: {e}"
        ) from e

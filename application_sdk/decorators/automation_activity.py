"""
Activity registration decorator for automatic registration with the automation engine.
"""

import inspect
import json
from typing import Any, Callable, Dict, List, Union, get_args, get_origin

from pydantic import BaseModel
import requests

from application_sdk.constants import AUTOMATION_ENGINE_API_URL
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# Global registry to collect decorated activities
ACTIVITY_SPECS: List[dict[str, Any]] = []


def _type_to_json_schema(annotation: Any) -> Dict[str, Any]:
    """Convert Python type annotation to JSON Schema using reflection."""
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        schema = annotation.model_json_schema(mode="serialization")
        if "$defs" in schema:
            defs = schema.pop("$defs")
            schema = {**schema, **defs}
        return schema

    origin = get_origin(annotation)
    if origin is Union:
        args = get_args(annotation)
        if type(None) in args:
            non_none_types = [arg for arg in args if arg is not type(None)]
            if non_none_types:
                schema = _type_to_json_schema(non_none_types[0])
                schema["nullable"] = True
                return schema

    if origin is dict or annotation is dict:
        args = get_args(annotation)
        if args:
            return {
                "type": "object",
                "additionalProperties": _type_to_json_schema(args[1]),
            }
        return {"type": "object"}

    if origin is list or annotation is list:
        args = get_args(annotation)
        if args:
            return {"type": "array", "items": _type_to_json_schema(args[0])}
        return {"type": "array"}

    type_mapping = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        Any: {},
    }

    if annotation in type_mapping:
        return type_mapping[annotation]

    return {}


def _generate_input_schema(func: Any) -> Dict[str, Any]:
    """Generate JSON Schema for function inputs using reflection."""
    sig = inspect.signature(func)
    properties = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue

        param_schema = (
            _type_to_json_schema(param.annotation)
            if param.annotation != inspect.Parameter.empty
            else {}
        )

        if param.default != inspect.Parameter.empty:
            if isinstance(param.default, (str, int, float, bool, type(None))):
                param_schema["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = param_schema

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return schema


def _generate_output_schema(func: Any) -> Dict[str, Any]:
    """Generate JSON Schema for function outputs using reflection."""
    return_annotation = inspect.signature(func).return_annotation
    if return_annotation == inspect.Signature.empty:
        return {}
    return _type_to_json_schema(return_annotation)


def automation_activity(
    name: str,
    description: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to mark an activity for automatic registration."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        input_schema: dict[str, Any] = _generate_input_schema(func)
        output_schema: dict[str, Any] = _generate_output_schema(func)

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

    logger.info(
        f"Registering {len(ACTIVITY_SPECS)} activities with automation engine"
    )

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

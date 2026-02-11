"""
Activity registration decorator for automatic registration with the automation engine.

This module provides the ``@automation_activity`` decorator that collects activity
metadata at import time and ``flush_activity_registrations()`` to push the collected
metadata to the automation engine's HTTP API.

All types required by the decorator (``ActivityCategory``, ``SubType``,
``ToolMetadata``, ``Parameter``, ``Annotation``, etc.) are defined in this module
so that consumers do not need to depend on the automation engine package.

Example usage::

    from application_sdk.decorators.automation_activity import (
        ActivityCategory,
        Annotation,
        Parameter,
        automation_activity,
        flush_activity_registrations,
    )
    from temporalio import activity

    @automation_activity(
        display_name="Say Hello",
        description="Greet someone by name.",
        inputs=[
            Parameter(name="name", description="Name", annotations=Annotation(display_name="Name")),
        ],
        outputs=[
            Parameter(name="message", description="Greeting", annotations=Annotation(display_name="Message")),
        ],
        category=ActivityCategory.UTILITY,
    )
    @activity.defn
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    # At startup, after the worker is running:
    await flush_activity_registrations(
        app_name="my-app",
        workflow_task_queue="atlan-my-app-local",
        automation_engine_api_url="http://localhost:8000",
    )
"""

import asyncio
import inspect
import os
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import aiohttp
from application_sdk.observability.logger_adaptor import get_logger
from pydantic import BaseModel, Field

logger = get_logger(__name__)


# =============================================================================
# Enums & lightweight models (moved from automation-engine to SDK)
# =============================================================================


class ActivityCategory(str, Enum):
    """Category of an automation activity."""

    DATA = "Data"
    FLOW = "Flow"
    PROCESSING = "Processing"
    OUTPUT = "Output"
    UTILITY = "Utility"


class SubType(Enum):
    """Subtype annotation for activity parameters."""

    FILE_PATH = "file_path"
    SQL = "sql"
    ROUTES = "routes"


class ToolMetadata(BaseModel):
    """Optional metadata for a registered tool/activity."""

    icon: Optional[str] = Field(default=None, description="Icon for the tool")


# =============================================================================
# Constants
# =============================================================================

# JSON Schema extension key used by the automation engine UI
X_AUTOMATION_ENGINE = "x-automation-engine"

# Automation engine API endpoints
ENDPOINT_SERVER_READY = "/server/ready"
ENDPOINT_APPS = "/api/v1/apps"
ENDPOINT_TOOLS = "/api/v1/tools"

# Timeouts (seconds)
TIMEOUT_HEALTH_CHECK = 5.0
TIMEOUT_API_REQUEST = 30.0

# Default prefix for computing app qualified names
APP_QUALIFIED_NAME_PREFIX = "default/apps/"

# Environment variable for the automation engine API URL
ENV_AUTOMATION_ENGINE_API_URL = "ATLAN_AUTOMATION_ENGINE_API_URL"


# =============================================================================
# SDK Pydantic models
# =============================================================================


class AppSpec(BaseModel):
    """App specification for SDK registration."""

    name: str = Field(description="Name of the app")
    qualified_name: Optional[str] = Field(
        default=None, description="Qualified name of the app"
    )
    task_queue: str = Field(description="Task queue for the app's workers")
    description: Optional[str] = Field(
        default=None, description="Description of the app"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="App metadata"
    )


class Annotation(BaseModel):
    """Annotation for an activity parameter (display hints for the UI)."""

    sub_type: Optional[SubType] = Field(
        default=None,
        description="Subtype of the parameter",
    )
    display_name: str = Field(description="Display name of the parameter")


class Parameter(BaseModel):
    """Describes a single input or output parameter of an activity."""

    name: str
    description: str
    annotations: Annotation
    schema_extra: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional JSON Schema constraints (minItems, maxItems, minLength, maxLength, minimum, maximum, pattern, etc.)",
    )


class ActivitySpec(BaseModel):
    """Collected metadata for a single decorated activity."""

    name: str = Field(description="Name of the activity")
    display_name: str = Field(description="Display name of the activity")
    description: str = Field(description="Description of the activity")
    input_schema: Dict[str, Any] = Field(
        description="Input schema as JSON Schema dictionary"
    )
    output_schema: Dict[str, Any] = Field(
        description="Output schema as JSON Schema dictionary"
    )
    category: ActivityCategory = Field(description="Category of the activity")
    examples: Optional[List[str]] = Field(
        default=None, description="Examples of usage"
    )
    metadata: Optional[ToolMetadata] = Field(
        default=None, description="Tool metadata"
    )


# =============================================================================
# Global registry
# =============================================================================

ACTIVITY_SPECS: List[ActivitySpec] = []
"""Global list that accumulates ``ActivitySpec`` objects as modules are imported."""


# =============================================================================
# Internal helpers – JSON Schema building
# =============================================================================


def _get_category_value(category: ActivityCategory) -> str:
    """Get string value from category enum."""
    return category.value if hasattr(category, "value") else str(category)


def _build_tool_dict(spec: ActivitySpec) -> Dict[str, Any]:
    """Build tool dictionary from ActivitySpec for API submission."""
    tool_dict: Dict[str, Any] = {
        "name": spec.name,
        "display_name": spec.display_name,
        "category": _get_category_value(spec.category),
        "description": spec.description,
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
    }
    if spec.examples is not None:
        tool_dict["examples"] = spec.examples
    if spec.metadata is not None:
        tool_dict["metadata"] = spec.metadata.model_dump(exclude_none=True)
    return tool_dict


def _build_schema_object(
    properties: Dict[str, Any], required: List[str]
) -> Dict[str, Any]:
    """Build a JSON schema object with properties and required fields."""
    return {"type": "object", "properties": properties, "required": required}


def _extract_and_hoist_defs(
    schema: Dict[str, Any], collected_defs: Dict[str, Any]
) -> None:
    """Recursively extract ``$defs`` from nested schemas and collect them.

    This mutates *schema* by removing ``$defs`` from nested locations, and
    collects them into *collected_defs* for hoisting to root level.
    """
    if not isinstance(schema, dict):
        return

    if "$defs" in schema:
        collected_defs.update(schema.pop("$defs"))

    for value in schema.values():
        if isinstance(value, dict):
            _extract_and_hoist_defs(value, collected_defs)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _extract_and_hoist_defs(item, collected_defs)


def _get_json_schema_type_from_hint(type_hint: Any) -> Dict[str, Any]:
    """Convert a Python type hint to a JSON Schema type definition."""
    origin = get_origin(type_hint)
    args = get_args(type_hint)

    # Handle Optional/Union types
    if origin in (Union, Optional):
        non_none_args = [arg for arg in args if arg not in (type(None), None)]
        if non_none_args:
            schema = _get_json_schema_type_from_hint(non_none_args[0])
            schema["nullable"] = True
            return schema

    # Handle List types
    if origin in (list, List):
        return {
            "type": "array",
            "items": _get_json_schema_type_from_hint(args[0] if args else Any),
        }

    # Handle Tuple types
    if origin in (tuple, Tuple):
        return {"type": "array", "items": {}}

    # Handle Pydantic models
    if inspect.isclass(type_hint) and issubclass(type_hint, BaseModel):
        return type_hint.model_json_schema()

    # Handle basic types
    type_mapping: Dict[Any, Any] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        Dict: "object",
        Any: {},
    }
    if type_hint in type_mapping:
        result = type_mapping[type_hint]
        return result if isinstance(result, dict) else {"type": result}

    return {"type": "object"}


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------


def _build_input_schema_from_parameters(
    parameters: List[Parameter], func: Callable[..., Any]
) -> Dict[str, Any]:
    """Build input schema from Parameter list."""
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    param_names = list(sig.parameters.keys())
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for param in parameters:
        param_name = param.name
        if param_name not in param_names:
            continue

        param_obj = sig.parameters[param_name]
        type_hint = type_hints.get(param_name, Any)
        prop_schema = _get_json_schema_type_from_hint(type_hint)
        prop_schema["description"] = param.description
        prop_schema[X_AUTOMATION_ENGINE] = param.annotations.model_dump(
            exclude_none=True, mode="json"
        )

        # Merge in schema_extra constraints (minItems, maxLength, etc.)
        if param.schema_extra:
            prop_schema.update(param.schema_extra)

        if param_obj.default != inspect.Parameter.empty:
            prop_schema["default"] = param_obj.default
        else:
            required.append(param_name)
        properties[param_name] = prop_schema

    schema = _build_schema_object(properties, required)
    schema["input_order"] = [
        param_name for param_name in param_names if param_name in properties
    ]

    collected_defs: Dict[str, Any] = {}
    _extract_and_hoist_defs(schema, collected_defs)
    if collected_defs:
        schema["$defs"] = collected_defs

    return schema


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


def _resolve_nested_model_reference(
    schema: Dict[str, Any], ref_schema: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Resolve a ``$ref`` to actual schema definition."""
    if "$ref" not in ref_schema:
        return None
    ref_path = ref_schema["$ref"].replace("#/$defs/", "")
    definitions = schema.get("$defs", {})
    return definitions.get(ref_path)


def _process_nested_model_fields(model_schema: Dict[str, Any]) -> None:
    """Process nested model fields to ensure annotations are properly formatted."""
    if "properties" not in model_schema:
        return

    for _field_name, field_schema in model_schema["properties"].items():
        has_annotation = X_AUTOMATION_ENGINE in field_schema
        is_nested_model = "properties" in field_schema
        is_model_reference = "$ref" in field_schema

        if not has_annotation:
            if is_nested_model:
                _process_nested_model_fields(field_schema)
            elif is_model_reference:
                nested_model = _resolve_nested_model_reference(
                    model_schema, field_schema
                )
                if nested_model:
                    _process_nested_model_fields(nested_model)
            continue

        annotation_data = field_schema[X_AUTOMATION_ENGINE]
        annotation = (
            Annotation(**annotation_data)
            if isinstance(annotation_data, dict)
            else annotation_data
        )
        field_schema[X_AUTOMATION_ENGINE] = annotation.model_dump(
            exclude_none=True, mode="json"
        )

        if is_nested_model:
            _process_nested_model_fields(field_schema)
        elif is_model_reference:
            nested_model = _resolve_nested_model_reference(
                model_schema, field_schema
            )
            if nested_model:
                _process_nested_model_fields(nested_model)


def _build_output_schema_from_parameters(
    parameters: List[Parameter], func: Callable[..., Any]
) -> Dict[str, Any]:
    """Build output schema from Parameter list."""
    type_hints = get_type_hints(func)
    return_type_hint = type_hints.get("return", Any)
    origin = get_origin(return_type_hint)
    is_tuple = origin in (tuple, Tuple)
    properties: Dict[str, Any] = {}
    required: List[str] = []

    if is_tuple:
        for param in parameters:
            prop_schema: Dict[str, Any] = {
                "type": "string",
                "description": param.description,
            }
            prop_schema[X_AUTOMATION_ENGINE] = param.annotations.model_dump(
                exclude_none=True, mode="json"
            )
            properties[param.name] = prop_schema
            required.append(param.name)
        schema = _build_schema_object(properties, required)
        schema["output_order"] = [param.name for param in parameters]
        return schema

    # Single return
    if not parameters:
        schema = _build_schema_object({}, [])
        schema["output_order"] = []
        return schema

    output_param = parameters[0]

    if inspect.isclass(return_type_hint) and issubclass(return_type_hint, BaseModel):
        model_schema = _get_json_schema_type_from_hint(return_type_hint)
        model_schema["description"] = output_param.description
        model_schema[X_AUTOMATION_ENGINE] = output_param.annotations.model_dump(
            exclude_none=True, mode="json"
        )
        _process_nested_model_fields(model_schema)
        properties[output_param.name] = model_schema
    else:
        prop_schema = _get_json_schema_type_from_hint(return_type_hint)
        prop_schema["description"] = output_param.description
        prop_schema[X_AUTOMATION_ENGINE] = output_param.annotations.model_dump(
            exclude_none=True, mode="json"
        )
        properties[output_param.name] = prop_schema

    required.append(output_param.name)
    schema = _build_schema_object(properties, required)
    schema["output_order"] = [param.name for param in parameters]

    collected_defs: Dict[str, Any] = {}
    _extract_and_hoist_defs(schema, collected_defs)
    if collected_defs:
        schema["$defs"] = collected_defs

    return schema


def _build_schema_from_parameters(
    parameters: List[Parameter], func: Callable[..., Any], *, is_input: bool = True
) -> Dict[str, Any]:
    """Build JSON schema from Parameter list."""
    if is_input:
        return _build_input_schema_from_parameters(parameters, func)
    return _build_output_schema_from_parameters(parameters, func)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_inputs_outputs(
    func: Callable[..., Any],
    inputs: List[Parameter],
    outputs: List[Parameter],
) -> None:
    """Validate that *inputs* / *outputs* match the function signature."""
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    param_names = list(sig.parameters.keys())

    # --- inputs ---
    if len(inputs) > 0:
        if len(inputs) != len(param_names):
            raise ValueError(
                f"Number of provided inputs ({len(inputs)}) doesn't match "
                f"function signature parameter count ({len(param_names)})"
            )
        missing = set(param_names) - {p.name for p in inputs}
        if missing:
            raise ValueError(
                f"Input parameter names don't match function signature. "
                f"Missing in decorator: {missing}"
            )
    elif len(param_names) > 0:
        raise ValueError(
            f"Function has {len(param_names)} parameters but inputs list is empty. "
            f"Provide Parameter objects for all parameters: {param_names}"
        )

    # --- outputs ---
    if len(outputs) > 0:
        return_type_hint = type_hints.get("return")
        if return_type_hint is None or return_type_hint is type(None):
            raise ValueError(
                "Provided outputs but function has no return annotation or returns None"
            )
        origin = get_origin(return_type_hint)
        is_tuple = origin in (tuple, Tuple)
        expected_count = len(get_args(return_type_hint)) if is_tuple else 1
        if len(outputs) != expected_count:
            raise ValueError(
                f"Number of provided outputs ({len(outputs)}) doesn't match "
                f"{'tuple' if is_tuple else 'single'} return type count ({expected_count})"
            )
    else:
        return_type_hint = type_hints.get("return")
        if return_type_hint is not None and return_type_hint is not type(None):
            raise ValueError(
                f"Function has return type {return_type_hint} but outputs list is empty. "
                "Provide Parameter objects for the return value or change return type to None."
            )


# =============================================================================
# Public API – decorator
# =============================================================================


def automation_activity(
    display_name: str,
    description: str,
    inputs: List[Parameter],
    outputs: List[Parameter],
    category: ActivityCategory,
    metadata: Optional[ToolMetadata] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to mark an activity for automatic registration with the automation engine.

    Decorated functions have their metadata collected into the module-level
    :data:`ACTIVITY_SPECS` list.  Call :func:`flush_activity_registrations`
    at application startup to push the collected metadata to the automation
    engine's HTTP API.

    Args:
        display_name: Human-readable display name of the activity.
        description: Description of what the activity does.
        inputs: List of :class:`Parameter` objects defining the input schema.
            Must match the function signature parameter names and count.
            Use an empty list ``[]`` if the function has no parameters.
        outputs: List of :class:`Parameter` objects defining the output schema.
            Must match the return type structure.
            Use an empty list ``[]`` if the function returns ``None``.
        category: Category of the activity.
        metadata: Optional metadata (e.g. icon) for the tool.

    Returns:
        The original function, unmodified.

    Raises:
        ValueError: If *inputs* / *outputs* don't match the function signature.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _validate_inputs_outputs(func, inputs, outputs)
        input_schema = _build_schema_from_parameters(inputs, func, is_input=True)
        output_schema = _build_schema_from_parameters(outputs, func, is_input=False)
        ACTIVITY_SPECS.append(
            ActivitySpec(
                name=func.__name__,
                display_name=display_name,
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                category=category,
                metadata=metadata,
            )
        )
        return func

    return decorator


# =============================================================================
# Public API – registration flush
# =============================================================================


def _resolve_automation_engine_api_url(
    automation_engine_api_url: Optional[str],
) -> Optional[str]:
    """Resolve the automation engine API URL from the argument or environment.

    Priority:
    1. Explicit ``automation_engine_api_url`` argument
    2. ``ATLAN_AUTOMATION_ENGINE_API_URL`` environment variable
    3. Constructed from ``ATLAN_AUTOMATION_ENGINE_API_HOST`` + ``ATLAN_AUTOMATION_ENGINE_API_PORT``
    """
    if automation_engine_api_url:
        return automation_engine_api_url

    env_url = os.environ.get(ENV_AUTOMATION_ENGINE_API_URL)
    if env_url:
        return env_url

    host = os.environ.get("ATLAN_AUTOMATION_ENGINE_API_HOST")
    port = os.environ.get("ATLAN_AUTOMATION_ENGINE_API_PORT")
    if host and port:
        return f"http://{host}:{port}"

    return None


def _resolve_app_qualified_name(
    app_qualified_name: Optional[str], app_name: str
) -> str:
    """Resolve the app qualified name from the argument or compute from app_name."""
    if app_qualified_name:
        return app_qualified_name

    env_qn = os.environ.get("ATLAN_APP_QUALIFIED_NAME")
    if env_qn:
        return env_qn

    # Compute: default/apps/<app_name_with_underscores>
    return f"{APP_QUALIFIED_NAME_PREFIX}{app_name.replace('-', '_').lower()}"


async def flush_activity_registrations(
    app_name: str,
    workflow_task_queue: str,
    automation_engine_api_url: Optional[str] = None,
    app_qualified_name: Optional[str] = None,
    activity_specs: Optional[List[ActivitySpec]] = None,
) -> None:
    """Push collected activity registrations to the automation engine HTTP API.

    This function consumes and clears the global :data:`ACTIVITY_SPECS` list
    (unless an explicit *activity_specs* is provided).

    Args:
        app_name: Name of the application registering activities.
        workflow_task_queue: Temporal task queue name for this app's workers.
        automation_engine_api_url: Full base URL of the automation engine API
            (e.g. ``http://localhost:8000``).  If not provided, falls back to
            the ``ATLAN_AUTOMATION_ENGINE_API_URL`` env var, or is constructed
            from ``ATLAN_AUTOMATION_ENGINE_API_HOST`` +
            ``ATLAN_AUTOMATION_ENGINE_API_PORT``.
        app_qualified_name: Qualified name for the app.  If not provided,
            falls back to the ``ATLAN_APP_QUALIFIED_NAME`` env var, or is
            computed as ``default/apps/<app_name>``.
        activity_specs: Explicit list of specs to register.  When ``None``
            the global ``ACTIVITY_SPECS`` is consumed and cleared.
    """
    # Snapshot and clear global specs
    specs_to_register = (
        activity_specs if activity_specs is not None else ACTIVITY_SPECS.copy()
    )
    if activity_specs is None:
        ACTIVITY_SPECS.clear()

    if not specs_to_register:
        logger.info("No activities to register")
        return

    base_url = _resolve_automation_engine_api_url(automation_engine_api_url)
    if not base_url:
        logger.warning(
            "Automation engine API URL not configured. "
            "Set ATLAN_AUTOMATION_ENGINE_API_URL or pass automation_engine_api_url. "
            "Skipping activity registration."
        )
        return

    qualified_name = _resolve_app_qualified_name(app_qualified_name, app_name)

    # Health check
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_HEALTH_CHECK)
        ) as session:
            async with session.get(
                f"{base_url}{ENDPOINT_SERVER_READY}"
            ) as response:
                response.raise_for_status()
                logger.info("Automation engine health check passed")
    except Exception as e:
        logger.warning(
            f"Automation engine health check failed: {e}. "
            "Skipping activity registration."
        )
        return

    logger.info(
        f"Registering {len(specs_to_register)} activities with automation engine"
    )

    # Upsert app
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_API_REQUEST)
        ) as session:
            async with session.post(
                f"{base_url}{ENDPOINT_APPS}",
                json={
                    "name": app_name,
                    "task_queue": workflow_task_queue,
                    "qualified_name": qualified_name,
                },
            ) as response:
                response.raise_for_status()
                logger.info(f"Successfully upserted app '{app_name}'")
    except Exception as e:
        logger.warning(f"Failed to upsert app '{app_name}': {e}")

    await asyncio.sleep(5)

    # Build and send tools
    tools = [_build_tool_dict(item) for item in specs_to_register]

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_API_REQUEST)
        ) as session:
            async with session.post(
                f"{base_url}{ENDPOINT_TOOLS}",
                json={
                    "app_qualified_name": qualified_name,
                    "app_name": app_name,
                    "task_queue": workflow_task_queue,
                    "tools": tools,
                },
            ) as response:
                response.raise_for_status()
                logger.info(
                    f"Successfully registered {len(tools)} activities "
                    "with automation engine"
                )
    except Exception as e:
        logger.warning(
            f"Failed to register activities with automation engine: {e}"
        )

"""JSON Schema building helpers for the automation activity decorator."""

import inspect
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

from pydantic import BaseModel

from application_sdk.decorators.automation_activity.models import (
    X_AUTOMATION_ENGINE,
    ActivityCategory,
    ActivitySpec,
    Annotation,
    Parameter,
    ToolSpec,
)

# Keys that schema_extra is allowed to set. Prevents callers from
# accidentally overwriting structural keys like "type" or "description".
_ALLOWED_SCHEMA_EXTRA_KEYS = frozenset(
    {
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "enum",
        "const",
        "multipleOf",
        "uniqueItems",
        "format",
    }
)


def _get_category_value(category: ActivityCategory) -> str:
    """Get string value from category enum."""
    return category.value if hasattr(category, "value") else str(category)


def _build_tool_spec(spec: ActivitySpec) -> ToolSpec:
    """Build a ``ToolSpec`` from an ``ActivitySpec`` for API submission."""
    return ToolSpec(
        name=spec.name,
        display_name=spec.display_name,
        category=_get_category_value(spec.category),
        description=spec.description,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        examples=spec.examples,
        metadata=spec.metadata.model_dump(exclude_none=True) if spec.metadata else None,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


def _validate_and_apply_schema_extra(
    param: Parameter, prop_schema: Dict[str, Any]
) -> None:
    """Validate and merge ``schema_extra`` into *prop_schema* in-place."""
    if not param.schema_extra:
        return
    disallowed = set(param.schema_extra) - _ALLOWED_SCHEMA_EXTRA_KEYS
    if disallowed:
        raise ValueError(
            f"Parameter '{param.name}' has disallowed schema_extra keys: "
            f"{disallowed}. Allowed keys: {sorted(_ALLOWED_SCHEMA_EXTRA_KEYS)}"
        )
    prop_schema.update(param.schema_extra)


def _stamp_param_metadata(param: Parameter, prop_schema: Dict[str, Any]) -> None:
    """Stamp description, automation-engine annotation, and schema_extra onto *prop_schema*.

    If *prop_schema* represents a Pydantic model (has ``"properties"``),
    nested field annotations are also normalised.
    """
    prop_schema["description"] = param.description
    prop_schema[X_AUTOMATION_ENGINE] = param.annotations.model_dump(
        exclude_none=True, mode="json"
    )
    _process_nested_model_fields(prop_schema)
    _validate_and_apply_schema_extra(param, prop_schema)


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


def _finalize_schema(
    properties: Dict[str, Any],
    required: List[str],
    order_key: str,
    order_values: List[str],
) -> Dict[str, Any]:
    """Build the schema object, set the order key, and hoist ``$defs``."""
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    schema[order_key] = order_values

    collected_defs: Dict[str, Any] = {}
    _extract_and_hoist_defs(schema, collected_defs)
    if collected_defs:
        schema["$defs"] = collected_defs

    return schema


# ---------------------------------------------------------------------------
# Nested model annotation processing
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
            nested_model = _resolve_nested_model_reference(model_schema, field_schema)
            if nested_model:
                _process_nested_model_fields(nested_model)


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
        prop_schema = _get_json_schema_type_from_hint(type_hints.get(param_name, Any))
        _stamp_param_metadata(param, prop_schema)

        if param_obj.default != inspect.Parameter.empty:
            prop_schema["default"] = param_obj.default
        else:
            required.append(param_name)
        properties[param_name] = prop_schema

    return _finalize_schema(
        properties,
        required,
        order_key="input_order",
        order_values=[n for n in param_names if n in properties],
    )


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


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
            prop_schema: Dict[str, Any] = {"type": "string"}
            _stamp_param_metadata(param, prop_schema)
            properties[param.name] = prop_schema
            required.append(param.name)
    elif parameters:
        output_param = parameters[0]
        prop_schema = _get_json_schema_type_from_hint(return_type_hint)
        _stamp_param_metadata(output_param, prop_schema)
        properties[output_param.name] = prop_schema
        required.append(output_param.name)

    return _finalize_schema(
        properties,
        required,
        order_key="output_order",
        order_values=[p.name for p in parameters],
    )


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def _build_schema_from_parameters(
    parameters: List[Parameter], func: Callable[..., Any], *, is_input: bool = True
) -> Dict[str, Any]:
    """Build JSON schema from Parameter list."""
    if is_input:
        return _build_input_schema_from_parameters(parameters, func)
    return _build_output_schema_from_parameters(parameters, func)

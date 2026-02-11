"""Input/output validation for the automation activity decorator."""

import inspect
from typing import Any, Callable, List, Tuple, get_args, get_origin, get_type_hints

from application_sdk.decorators.automation_activity.models import Parameter


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

"""Input/output validation for the automation activity decorator."""

import inspect
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Set,
    Tuple,
    get_args,
    get_origin,
    get_type_hints,
)

from application_sdk.decorators.automation_activity.models import Parameter


def _validate_params(
    params: List[Parameter],
    expected_count: int,
    label: str,
    expected_names: Optional[Set[str]] = None,
) -> None:
    """Validate a list of ``Parameter`` objects against an expected count and optional names.

    Args:
        params: The decorator-supplied parameter list.
        expected_count: How many parameters the function signature expects.
        label: ``"input"`` or ``"output"`` — used in error messages.
        expected_names: If provided, parameter names must cover this set exactly.
    """
    if params:
        if len(params) != expected_count:
            raise ValueError(
                f"Number of provided {label}s ({len(params)}) doesn't match "
                f"expected count ({expected_count})"
            )
        if expected_names:
            missing = expected_names - {p.name for p in params}
            if missing:
                raise ValueError(
                    f"{label.capitalize()} parameter names don't match function signature. "
                    f"Missing in decorator: {missing}"
                )
    elif expected_count > 0:
        raise ValueError(
            f"Expected {expected_count} {label}(s) but {label}s list is empty."
        )


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
    _validate_params(
        params=inputs,
        expected_count=len(param_names),
        label="input",
        expected_names=set(param_names),
    )

    # --- outputs ---
    return_type_hint = type_hints.get("return")
    has_return = return_type_hint is not None and return_type_hint is not type(None)

    if has_return:
        origin = get_origin(return_type_hint)
        is_tuple = origin in (tuple, Tuple)
        expected_output_count = len(get_args(return_type_hint)) if is_tuple else 1
    else:
        expected_output_count = 0

    if outputs and not has_return:
        raise ValueError(
            "Provided outputs but function has no return annotation or returns None"
        )

    _validate_params(
        params=outputs,
        expected_count=expected_output_count,
        label="output",
    )

"""
Activity registration decorator for automatic registration with the automation engine.

This module provides the ``@automation_activity`` decorator that collects activity
metadata at import time and ``flush_activity_registrations()`` to push the collected
metadata to the automation engine's HTTP API.

All types required by the decorator (``ActivityCategory``, ``SubType``,
``ToolMetadata``, ``Parameter``, ``Annotation``, etc.) are defined in this module
so that consumers do not need to depend on the automation engine package.

Example usage::

    from application_sdk.decorators.automation_activity.models import (
        ActivityCategory,
        Annotation,
        Parameter,
    )
    from application_sdk.decorators import (
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

import threading
from contextlib import contextmanager
from typing import Any, Callable, List, Optional

from application_sdk.decorators.automation_activity.models import (
    ActivityCategory,
    ActivitySpec,
    Parameter,
    ToolMetadata,
)
from application_sdk.decorators.automation_activity.registration import MAX_RETRIES, _flush_specs
from application_sdk.decorators.automation_activity.schema import _build_schema_from_parameters
from application_sdk.decorators.automation_activity.validation import _validate_inputs_outputs


# =============================================================================
# Global registry (W2: thread-safe)
# =============================================================================

_specs_lock = threading.Lock()
"""Lock protecting mutations to :data:`ACTIVITY_SPECS`."""

ACTIVITY_SPECS: List[ActivitySpec] = []
"""Global list that accumulates ``ActivitySpec`` objects as modules are imported.

.. note::

   This list is populated at import time by the ``@automation_activity``
   decorator.  If you import decorated modules in tests, scripts, or
   notebooks, use :func:`isolated_activity_specs` to avoid polluting the
   global registry.
"""


# =============================================================================
# Test isolation (T2)
# =============================================================================


@contextmanager
def isolated_activity_specs():
    """Context manager that saves/restores ACTIVITY_SPECS for test isolation.

    Use this in tests or scripts that import modules with
    ``@automation_activity`` decorators to prevent polluting the global
    registry::

        with isolated_activity_specs() as specs:
            import my_module  # decorated activities go into ``specs``
            assert len(specs) > 0
        # ACTIVITY_SPECS is restored to its previous state
    """
    with _specs_lock:
        saved = ACTIVITY_SPECS.copy()
        ACTIVITY_SPECS.clear()
    try:
        yield ACTIVITY_SPECS
    finally:
        with _specs_lock:
            ACTIVITY_SPECS.clear()
            ACTIVITY_SPECS.extend(saved)


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
        spec = ActivitySpec(
            name=func.__name__,
            display_name=display_name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            category=category,
            metadata=metadata,
        )
        with _specs_lock:
            ACTIVITY_SPECS.append(spec)
        return func

    return decorator


# =============================================================================
# Public API – registration flush
# =============================================================================


async def flush_activity_registrations(
    app_name: str,
    workflow_task_queue: str,
    automation_engine_api_url: Optional[str] = None,
    app_qualified_name: Optional[str] = None,
    activity_specs: Optional[List[ActivitySpec]] = None,
    max_retries: int = MAX_RETRIES,
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
        max_retries: Maximum number of retries for transient HTTP failures.
            Defaults to 3.
    """
    if activity_specs is not None:
        specs_to_register = activity_specs
    else:
        with _specs_lock:
            specs_to_register = ACTIVITY_SPECS.copy()
            ACTIVITY_SPECS.clear()

    await _flush_specs(
        specs_to_register=specs_to_register,
        app_name=app_name,
        workflow_task_queue=workflow_task_queue,
        automation_engine_api_url=automation_engine_api_url,
        app_qualified_name=app_qualified_name,
        max_retries=max_retries,
    )

"""Private helpers for App entry-point collection, validation, and registration.

These are extracted from base.py so that ``App.__init_subclass__`` stays a
thin dispatcher. Nothing in this module is part of the public SDK surface.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, get_type_hints

from application_sdk.app.entrypoint import (
    EntryPointContractError,
    EntryPointMetadata,
    get_entrypoint_metadata,
    is_entrypoint,
)
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import get_task_metadata, is_task
from application_sdk.contracts.base import Input, Output
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.app.base import App

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Task registration
# ---------------------------------------------------------------------------


def _register_tasks(cls: type, app_name: str) -> None:
    """Register all @task decorated methods for an App class.

    Args:
        cls: The App class.
        app_name: The app's registered name.
    """
    task_registry = TaskRegistry.get_instance()
    for attr_name in dir(cls):
        if attr_name.startswith("_"):
            continue
        attr = getattr(cls, attr_name, None)
        if attr is None:
            continue
        if is_task(attr):
            task_meta = get_task_metadata(attr)
            if task_meta:
                task_meta_copy = replace(task_meta, app_name=app_name)
                task_registry.register(app_name, task_meta_copy)


# ---------------------------------------------------------------------------
# Entry-point collection
# ---------------------------------------------------------------------------


def _collect_implicit_ep(
    cls: type,
    app_run_stub: Any,
) -> EntryPointMetadata | None:
    """Collect the implicit run() entry point from an App class, if present.

    Args:
        cls: The App subclass being registered.
        app_run_stub: The unoverridden ``App.run`` — used to detect whether
            the subclass has a concrete override.

    Returns:
        An ``EntryPointMetadata`` for run(), or ``None`` when run() is absent,
        abstract, or has unresolvable annotations.

    Raises:
        EntryPointContractError: If run() annotations are present but invalid.
    """
    if "run" in cls.__dict__:
        run_method: Any = cls.__dict__["run"]
    elif cls.run is not app_run_stub:
        run_method = cls.run
    else:
        run_method = None

    if run_method is None or getattr(run_method, "__isabstractmethod__", False):
        return None

    try:
        hints = get_type_hints(cls.run)
    except Exception:
        _logger.warning(
            "Skipping run() as entry point for %s: get_type_hints failed "
            "to resolve annotations (likely an unresolved forward reference "
            "or missing import). run() will not be included as an entry point.",
            cls.__name__,
            exc_info=True,
        )
        return None

    input_type = hints.get("input")
    output_type = hints.get("return")

    if input_type is None or output_type is None:
        raise EntryPointContractError(
            f"run() on {cls.__name__} must have type annotations: "
            f"async def run(self, input: <Input subclass>) -> <Output subclass>"
        )
    if not (isinstance(input_type, type) and issubclass(input_type, Input)):
        raise EntryPointContractError(
            f"run() on {cls.__name__}: input type {input_type!r} must be "
            f"a subclass of Input."
        )
    if not (isinstance(output_type, type) and issubclass(output_type, Output)):
        raise EntryPointContractError(
            f"run() on {cls.__name__}: output type {output_type!r} must be "
            f"a subclass of Output."
        )
    if input_type is Input:
        raise EntryPointContractError(
            f"run() on {cls.__name__}: input type must be a concrete subclass "
            f"of Input, not Input itself. Define a dedicated dataclass."
        )
    if output_type is Output:
        raise EntryPointContractError(
            f"run() on {cls.__name__}: output type must be a concrete subclass "
            f"of Output, not Output itself. Define a dedicated dataclass."
        )

    return EntryPointMetadata(
        name="run",
        input_type=input_type,
        output_type=output_type,
        method_name="run",
        implicit=True,
        default=False,  # adjusted by _build_entry_points when mixed with explicit eps
    )


def _scan_entrypoints(cls: type) -> dict[str, EntryPointMetadata]:
    """Scan a class for @entrypoint-decorated methods.

    Returns a dict keyed by entry-point name, sorted alphabetically by name
    so that auto-default selection is deterministic and matches documentation.

    Args:
        cls: The App class to scan.

    Returns:
        Dict mapping entry point name to EntryPointMetadata, sorted by name.
    """
    entry_points: dict[str, EntryPointMetadata] = {}
    for attr_name in dir(cls):
        if attr_name.startswith("_"):
            continue
        attr = getattr(cls, attr_name, None)
        if attr is None:
            continue
        if is_entrypoint(attr):
            ep_meta = get_entrypoint_metadata(attr)
            if ep_meta is not None:
                entry_points[ep_meta.name] = ep_meta
    return dict(sorted(entry_points.items()))


# ---------------------------------------------------------------------------
# Entry-point building and validation
# ---------------------------------------------------------------------------


def _build_entry_points(
    cls: type,
    implicit_ep: EntryPointMetadata | None,
    explicit_eps: dict[str, EntryPointMetadata],
) -> dict[str, EntryPointMetadata]:
    """Build and validate the combined entry-points dict for an App subclass.

    Handles four layout combinations:

    * run()-only — implicit ep, no explicit eps.
    * @entrypoint(s)-only — explicit eps, no implicit ep.
    * Mixed run() + @entrypoint(s) — run() always holds the default.
    * Empty — neither; returns ``{}`` so caller skips registration.

    Args:
        cls: The App subclass (used only for error messages).
        implicit_ep: The run() entry point, or ``None`` if absent.
        explicit_eps: Explicit ``@entrypoint``-decorated methods keyed by ep name.

    Returns:
        Combined ``{name: EntryPointMetadata}`` dict, empty when nothing to register.

    Raises:
        EntryPointContractError: For invalid configurations.
    """
    if not explicit_eps and implicit_ep is None:
        return {}

    entry_points: dict[str, EntryPointMetadata] = {}

    if implicit_ep is not None:
        if explicit_eps:
            # Mixed: run() always has default-precedence. Using
            # @entrypoint(default=True) alongside run() is prohibited.
            explicit_defaults = [ep.name for ep in explicit_eps.values() if ep.default]
            if explicit_defaults:
                raise EntryPointContractError(
                    f"{cls.__name__}: cannot use @entrypoint(default=True) when "
                    f"run() is also implemented — run() always has default-precedence "
                    f"in mixed apps. Remove default=True from "
                    f"{sorted(explicit_defaults)} or remove the run() override."
                )
        # run() is default=True in mixed mode; run()-only apps leave it False
        # and rely on _resolve_default_entrypoint's len==1 path.
        implicit_ep = replace(implicit_ep, default=bool(explicit_eps))
        entry_points["run"] = implicit_ep

    # Validate and add every explicit @entrypoint.
    for ep in explicit_eps.values():
        if not (isinstance(ep.input_type, type) and issubclass(ep.input_type, Input)):
            raise EntryPointContractError(
                f"Entry point '{ep.name}' on {cls.__name__}: "
                f"input type {ep.input_type!r} must be a subclass of Input."
            )
        if not (
            isinstance(ep.output_type, type) and issubclass(ep.output_type, Output)
        ):
            raise EntryPointContractError(
                f"Entry point '{ep.name}' on {cls.__name__}: "
                f"output type {ep.output_type!r} must be a subclass of Output."
            )
        entry_points[ep.name] = ep

    # For @entrypoint-only apps with multiple entry points and no explicit default,
    # auto-mark the alphabetically-first by ep.name so _resolve_default_entrypoint
    # always finds exactly one.
    if implicit_ep is None and len(entry_points) > 1:
        if not any(ep.default for ep in entry_points.values()):
            first_name = min(entry_points)
            entry_points[first_name] = replace(entry_points[first_name], default=True)

    # At most one entry point may be the default.
    defaults = [ep.name for ep in entry_points.values() if ep.default]
    if len(defaults) > 1:
        raise EntryPointContractError(
            f"{cls.__name__}: more than one default entry point "
            f"({sorted(defaults)}). At most one entry point may be the default — "
            f"mark one @entrypoint(default=True) or let run() remain the default."
        )

    return entry_points


# ---------------------------------------------------------------------------
# App registration
# ---------------------------------------------------------------------------


def _apply_app_registration(
    cls: type[App],
    name: str,
    version: str,
    description: str,
    tags: dict[str, str] | None,
    passthrough_modules: set[str] | None,
    input_type: type[Input],
    output_type: type[Output],
    entry_points: dict[str, EntryPointMetadata] | None = None,
) -> None:
    """Register an App class with the AppRegistry.

    Args:
        cls: The App class to register.
        name: App name.
        version: Semantic version string.
        description: Human-readable description.
        tags: Optional tags for categorization.
        passthrough_modules: Modules to pass through sandbox.
        input_type: Input dataclass type.
        output_type: Output dataclass type.
        entry_points: Entry point metadata keyed by entry point name.
    """
    cls._app_registered = True
    cls._app_name = name
    cls._app_version = version
    cls._input_type = input_type
    cls._output_type = output_type

    registry = AppRegistry.get_instance()
    metadata = registry.register(
        name=name,
        version=version,
        app_cls=cls,
        input_type=input_type,
        output_type=output_type,
        description=description,
        tags=tags,
        passthrough_modules=passthrough_modules,
        entry_points=entry_points or {},
        allow_override=True,
    )
    cls._app_metadata = metadata

    _register_tasks(cls, name)

"""Entry point decorator for defining independently-triggerable execution paths in Apps.

Entry points generate Temporal workflows at worker startup. Each entry point can be
triggered independently via HTTP POST /workflows/v1/start?entrypoint=<name>.
The body field 'workflow_type' is also accepted as a transitional fallback.

Usage::

    from application_sdk.app import App, entrypoint, task
    from dataclasses import dataclass
    from application_sdk.contracts.base import Input, Output

    @dataclass
    class ExtractionInput(Input):
        connection_qualified_name: str = ""

    @dataclass
    class ExtractionOutput(Output):
        count: int = 0

    @dataclass
    class MiningInput(Input):
        connection_qualified_name: str = ""

    @dataclass
    class MiningOutput(Output):
        count: int = 0

    class SnowflakeApp(App):
        @entrypoint
        async def extract_metadata(self, input: ExtractionInput) -> ExtractionOutput:
            ...

        @entrypoint
        async def mine_queries(self, input: MiningInput) -> MiningOutput:
            ...
"""

import inspect
from dataclasses import dataclass
from typing import Any, Callable, TypeVar, get_type_hints

from application_sdk.contracts.base import Input, Output
from application_sdk.errors import CONTRACT_VALIDATION, ErrorCode

F = TypeVar("F", bound=Callable[..., Any])


class EntryPointContractError(Exception):
    """Raised when an entry point's contract is invalid."""

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code or CONTRACT_VALIDATION

    def __str__(self) -> str:
        return f"[{self.error_code}] {super().__str__()}"


@dataclass
class EntryPointMetadata:
    """Metadata about a registered entry point.

    Entry points are independently-triggerable execution paths on an App.
    Each entry point generates one Temporal workflow at worker startup.
    """

    name: str
    """Kebab-case name used in workflow_type dispatch (e.g. 'extract-metadata')."""

    input_type: type[Input]
    """The Input dataclass type for this entry point."""

    output_type: type[Output]
    """The Output dataclass type for this entry point."""

    method_name: str
    """The actual method name on the App class (e.g. 'extract_metadata')."""

    implicit: bool = False
    """True if derived from run() for backward-compat single-entry-point apps."""


def _method_name_to_kebab(name: str) -> str:
    """Convert 'extract_metadata' to 'extract-metadata'."""
    return name.replace("_", "-")


def _validate_entrypoint_signature(
    fn: Callable[..., Any],
) -> tuple[type[Input], type[Output]]:
    """Validate and extract Input/Output types from an entry point method.

    Entry points must follow the single-dataclass contract pattern:
    - Exactly one parameter (besides self) extending Input
    - Return type extending Output

    Args:
        fn: The entry point function to validate.

    Returns:
        Tuple of (input_type, output_type).

    Raises:
        EntryPointContractError: If the signature is invalid.
    """
    fn_name = getattr(fn, "__name__", repr(fn))

    sig = inspect.signature(fn)
    params = list(sig.parameters.values())

    if params and params[0].name == "self":
        params = params[1:]

    if len(params) != 1:
        raise EntryPointContractError(
            f"Entry point '{fn_name}' must have exactly one parameter (extending Input), "
            f"got {len(params)} parameters. "
            f"Wrap multiple values in a single Input dataclass."
        )

    try:
        hints = get_type_hints(fn)
    except Exception:
        raw: dict[str, Any] = getattr(fn, "__annotations__", {})
        unresolvable = [k for k, v in raw.items() if isinstance(v, str)]
        if unresolvable:
            raise EntryPointContractError(
                f"Entry point '{fn_name}' has unresolvable annotations for {unresolvable}. "
                "This usually happens when 'from __future__ import annotations' is "
                "used alongside Input/Output types that are not defined at module level."
            ) from None
        hints = raw

    param = params[0]
    input_type = hints.get(param.name)
    if input_type is None:
        raise EntryPointContractError(
            f"Entry point '{fn_name}' parameter '{param.name}' must have a type "
            f"annotation extending Input."
        )

    if not (isinstance(input_type, type) and issubclass(input_type, Input)):
        raise EntryPointContractError(
            f"Entry point '{fn_name}' parameter '{param.name}' must extend Input "
            f"base class, got {input_type}."
        )

    output_type = hints.get("return")
    if output_type is None:
        raise EntryPointContractError(
            f"Entry point '{fn_name}' must have a return type annotation extending Output."
        )

    if not (isinstance(output_type, type) and issubclass(output_type, Output)):
        raise EntryPointContractError(
            f"Entry point '{fn_name}' return type must extend Output base class, "
            f"got {output_type}."
        )

    return input_type, output_type


def entrypoint(
    func: F | None = None,
    *,
    name: str | None = None,
) -> F | Callable[[F], F]:
    """Decorator to mark a method as an independently-triggerable entry point.

    Each entry point generates one Temporal workflow at worker startup. Multiple
    entry points on the same App share @task methods as Temporal activities.

    Entry points are triggered via HTTP POST /workflows/v1/start?entrypoint=<name>.
    The body field 'workflow_type' is also accepted as a transitional fallback.

    Workflow naming:
    - Single-entry-point apps: ``{app-name}`` (backward compat, no colon)
    - Multi-entry-point apps: ``{app-name}:{entry-point-name}``

    Example::

        class SnowflakeApp(App):

            @entrypoint
            async def extract_metadata(
                self, input: ExtractionInput
            ) -> ExtractionOutput:
                databases = await self.fetch_databases(...)
                ...
                return ExtractionOutput(count=n)

            @entrypoint
            async def mine_queries(
                self, input: MiningInput
            ) -> MiningOutput:
                batches = await self.get_query_batches(...)
                ...
                return MiningOutput(count=n)

    Args:
        func: The function to decorate (when used without parentheses).
        name: Override the entry point name (defaults to method name in kebab-case).
            Useful for Argo DAG compatibility.

    Raises:
        EntryPointContractError: If the method doesn't follow the contract pattern.
    """

    def decorator(fn: F) -> F:
        fn_name = getattr(fn, "__name__", repr(fn))
        ep_name = name or _method_name_to_kebab(fn_name)
        # Validate custom name is a safe identifier (defense-in-depth: it becomes
        # part of a dynamically generated class name and a Temporal workflow name).
        if name is not None and not ep_name.replace("-", "_").isidentifier():
            raise EntryPointContractError(
                f"Entry point name '{ep_name}' is not a valid identifier. "
                "Use only letters, digits, hyphens, and underscores."
            )
        input_type, output_type = _validate_entrypoint_signature(fn)
        fn._entrypoint_metadata = EntryPointMetadata(  # type: ignore[attr-defined]
            name=ep_name,
            input_type=input_type,
            output_type=output_type,
            method_name=fn_name,
        )
        return fn

    if func is not None:
        return decorator(func)
    return decorator


def is_entrypoint(obj: Any) -> bool:
    """Check if an object is decorated with @entrypoint."""
    return hasattr(obj, "_entrypoint_metadata")


def get_entrypoint_metadata(obj: Any) -> EntryPointMetadata | None:
    """Get entry point metadata from a decorated function."""
    return getattr(obj, "_entrypoint_metadata", None)

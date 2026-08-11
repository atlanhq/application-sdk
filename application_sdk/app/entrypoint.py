"""Entry point decorator for defining independently-triggerable execution paths in Apps.

Entry points generate Temporal workflows at worker startup. Each entry point can be
triggered independently via HTTP POST /workflows/v1/start?entrypoint=<name>.
The body field 'workflow_type' is also accepted as a transitional fallback.

Default entrypoint resolution (when ?entrypoint= is omitted):

    App shape                                   Default
    ------------------------------------------  ----------------------------------------
    run() only                                  run() — implicit default (backward compat)
    Single @entrypoint                          that entry point (len==1 rule)
    Multiple @entrypoints, none explicit        first alphabetically, auto-marked default
    Multiple @entrypoints, one default=True     that one
    Multiple @entrypoints, multiple default=True  error at class definition time
    run() + @entrypoint(s)                      run() always; @entrypoint(default=True) raises

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
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar, get_type_hints

from application_sdk.contracts.base import Input, Output
from application_sdk.errors import CONTRACT_VALIDATION, ErrorCode
from application_sdk.errors.leaves import InvalidInputError

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(kw_only=True)
class UnresolvableEntrypointAnnotationsError(InvalidInputError):
    """Entry point has string annotations that cannot be resolved at decoration time."""

    code: ClassVar[str] = "INVALID_INPUT_ENTRYPOINT_UNRESOLVABLE_ANNOTATIONS"
    field: str | None = "annotations"


class EntryPointContractError(InvalidInputError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    code: ClassVar[str] = "INVALID_INPUT_ENTRYPOINT_CONTRACT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        warnings.warn(
            "EntryPointContractError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        InvalidInputError.__init__(self, message=message)
        self._legacy_error_code = error_code or CONTRACT_VALIDATION

    @property
    def error_code(self) -> ErrorCode:
        return self._legacy_error_code

    def __str__(self) -> str:
        return f"[{self._legacy_error_code}] {self.message}"


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

    default: bool = False
    """True if this entry point is the app's default — used when a caller does
    not specify ``?entrypoint=``. A single-entry-point app's only entry point is
    always treated as the default regardless of this flag; for multi-entry-point
    apps, at most one entry point may set ``default=True`` (validated at
    registration)."""

    workflow_type: str | None = None
    """Temporal workflow type to register instead of the ``{app-name}:{name}``
    convention. Moves Temporal registration only — the entry point is still
    selected by ``name`` on ``?entrypoint=``, and task activity names are
    unaffected. The canonical name stays registered as an alias, so callers on
    either name reach this entry point. See :func:`workflow_types_for`."""


def _method_name_to_kebab(name: str) -> str:
    """Convert 'extract_metadata' to 'extract-metadata'."""
    return name.replace("_", "-")


def entrypoint_module_segment(name: str) -> str:
    """Convert a kebab-case entry-point name to its Python module segment.

    Entry-point names are kebab-case on the wire and on disk (the
    ``app/generated/<name>/`` contract dirs and the ``connector`` identifier),
    but each entry point's hand-written code lives under a snake_case package
    (``app.<segment>.core`` / ``app.<segment>.handler``). This is the single
    canonical kebab→snake conversion for entry-point names — entry-point
    registration here and the per-entry-point handler dispatch in
    :mod:`application_sdk.handler.service` both route through it.

    Example::

        entrypoint_module_segment("asset-export-advanced") → "asset_export_advanced"
    """
    return name.replace("-", "_")


def workflow_type_class_segment(workflow_type: str) -> str:
    """Convert a Temporal workflow type into its generated-class name segment.

    Each registered type produces a dynamically generated ``_Workflow_<segment>``
    class, so the type must survive this conversion as a valid identifier.
    Temporal itself puts no charset restriction on a workflow type, so every
    character that cannot appear in an identifier is folded to ``_`` rather than
    rejected — otherwise a legacy type registered by a Java or Go worker
    (``com.acme.MyWorkflow``) could not be preserved, which is the case this
    exists for. Distinct types that fold to one segment are rejected at
    registration by :func:`build_workflow_type_index`, so the folding cannot
    silently merge two workflows.

    Example::

        workflow_type_class_segment("query-intelligence:keifu") → "query_intelligence_keifu"
        workflow_type_class_segment("com.acme.MyWorkflow")      → "com_acme_MyWorkflow"
    """
    return "".join(
        char if char.isalnum() or char == "_" else "_" for char in workflow_type
    )


def canonical_workflow_type(app_name: str, ep: EntryPointMetadata) -> str:
    """The convention-derived Temporal workflow type for *ep*.

    ``{app-name}`` for the implicit (run()-derived) entry point, and
    ``{app-name}:{entry-point-name}`` for every explicit ``@entrypoint``.
    """
    return app_name if ep.implicit else f"{app_name}:{ep.name}"


def workflow_types_for(app_name: str, ep: EntryPointMetadata) -> tuple[str, ...]:
    """Every Temporal workflow type *ep* registers, primary first.

    Without an override this is just the canonical name. With one, the override
    is primary — new runs start on it — and the canonical name stays registered
    as an alias so a caller already using it still reaches this entry point.
    An override that merely restates the canonical name registers once.
    """
    canonical = canonical_workflow_type(app_name, ep)
    if ep.workflow_type is None or ep.workflow_type == canonical:
        return (canonical,)
    return (ep.workflow_type, canonical)


def primary_workflow_type(app_name: str, ep: EntryPointMetadata) -> str:
    """The Temporal workflow type new runs of *ep* should start on."""
    return workflow_types_for(app_name, ep)[0]


def build_workflow_type_index(
    app_name: str, entry_points: Mapping[str, EntryPointMetadata]
) -> dict[str, EntryPointMetadata]:
    """Map every registered Temporal workflow type back to its entry point.

    The worker registers one generated class per key, and result-type resolution
    reads the same index — so what is registered and what can be resolved cannot
    drift apart.

    Uniqueness is enforced on two axes. The Temporal type is the registration
    key, but dispatch under the sandbox goes through the generated class name,
    which folds both ``-`` and ``:`` to ``_``. So ``qi:bar`` and ``qi-bar`` are
    distinct types whose classes would overwrite each other in the module
    namespace, and one would silently run the other's entry point.

    Raises:
        EntryPointContractError: If two entry points claim the same type, or if
            two distinct types fold to the same generated class name. The first
            also covers an override colliding with another entry point's
            canonical name or with the implicit bare app name, since every such
            name is a key here.
    """
    index: dict[str, EntryPointMetadata] = {}
    by_class_segment: dict[str, str] = {}
    for ep in entry_points.values():
        for workflow_type in workflow_types_for(app_name, ep):
            claimed = index.get(workflow_type)
            if claimed is not None and claimed is not ep:
                raise EntryPointContractError(
                    f"App '{app_name}': entry points '{claimed.name}' and "
                    f"'{ep.name}' both register Temporal workflow type "
                    f"'{workflow_type}'. Each registered type must resolve to "
                    f"exactly one entry point — change one @entrypoint's "
                    f"workflow_type."
                )
            segment = workflow_type_class_segment(workflow_type)
            twin = by_class_segment.get(segment)
            if twin is not None and twin != workflow_type:
                raise EntryPointContractError(
                    f"App '{app_name}': Temporal workflow types '{twin}' and "
                    f"'{workflow_type}' both generate the workflow class "
                    f"'_Workflow_{segment}', so one would silently dispatch to "
                    f"the other. Hyphens and colons both become underscores — "
                    f"pick a workflow_type that differs by more than those."
                )
            by_class_segment[segment] = workflow_type
            index[workflow_type] = ep
    return index


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
    except NameError:
        raw: dict[str, Any] = getattr(fn, "__annotations__", {})
        unresolvable = [k for k, v in raw.items() if isinstance(v, str)]
        if unresolvable:
            raise UnresolvableEntrypointAnnotationsError(
                message=(
                    f"Entry point '{fn_name}' has unresolvable annotations for {unresolvable}. "
                    "This usually happens when 'from __future__ import annotations' is "
                    "used alongside Input/Output types that are not defined at module level."
                ),
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


def _validate_workflow_type_override(ep_name: str, workflow_type: str) -> None:
    """Reject a workflow_type override that cannot be registered.

    Deliberately looser than the entry-point name check. Temporal puts no
    charset restriction on a workflow type, and the shapes a migrating app must
    preserve are varied — ``teradata-app:crawler``, ``9to5Workflow``,
    ``com.acme.MyWorkflow``. So this rejects only what carries no identifying
    content at all; :func:`workflow_type_class_segment` folds the rest into a
    usable class name, and :func:`build_workflow_type_index` rejects two types
    that fold together.

    Two things are still refused. Whitespace and control characters would fold
    to ``_`` like anything else and be accepted silently, yet they are never
    part of an established type and a control character mangles logs and the
    Temporal UI. A type with no alphanumeric content at all folds to an
    indistinguishable run of underscores. A leading digit is fine — the type is
    embedded in ``_Workflow_<segment>``.
    """
    if not isinstance(workflow_type, str):
        raise EntryPointContractError(
            f"Entry point '{ep_name}': workflow_type must be a string, got "
            f"{type(workflow_type).__name__}."
        )
    if not workflow_type:
        raise EntryPointContractError(
            f"Entry point '{ep_name}': workflow_type must be a non-empty string."
        )
    if any(char.isspace() or not char.isprintable() for char in workflow_type):
        raise EntryPointContractError(
            f"Entry point '{ep_name}': workflow_type must not contain whitespace "
            f"or control characters, got {workflow_type!r}."
        )
    if not any(char.isalnum() for char in workflow_type):
        raise EntryPointContractError(
            f"Entry point '{ep_name}': workflow_type {workflow_type!r} is not a "
            f"usable Temporal workflow type — it carries no letters or digits."
        )


def entrypoint(
    func: F | None = None,
    *,
    name: str | None = None,
    default: bool = False,
    workflow_type: str | None = None,
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
        default: Mark this entry point as the app's default — resolved when a
            caller omits ``?entrypoint=``. At most one entry point per app may set
            this (validated at registration). A single-entry-point app does not
            need it; its only entry point is the default implicitly.
        workflow_type: Register this Temporal workflow type instead of the
            ``{app-name}:{name}`` convention. Use it only to preserve a workflow
            type external callers already dispatch — a multi-entry-point app
            otherwise has no way to keep an established bare type. The canonical
            name stays registered as an alias, so adopting this cannot break a
            caller already on it. Registration is all that moves: ``?entrypoint=``
            still selects by ``name``, and task activity names are unaffected.

            Example::

                @entrypoint(name="keifu", workflow_type="KeifuWorkflow")
                async def keifu(self, input: KeifuInput) -> KeifuOutput: ...

    Raises:
        EntryPointContractError: If the method doesn't follow the contract pattern.
    """

    def decorator(fn: F) -> F:
        fn_name = getattr(fn, "__name__", repr(fn))
        ep_name = name or _method_name_to_kebab(fn_name)
        # Validate custom name is a safe identifier (defense-in-depth: it becomes
        # part of a dynamically generated class name and a Temporal workflow name).
        if name is not None and not entrypoint_module_segment(ep_name).isidentifier():
            raise EntryPointContractError(
                f"Entry point name '{ep_name}' is not a valid identifier. "
                "Use only letters, digits, hyphens, and underscores."
            )
        if workflow_type is not None:
            _validate_workflow_type_override(ep_name, workflow_type)
        input_type, output_type = _validate_entrypoint_signature(fn)
        fn._entrypoint_metadata = EntryPointMetadata(  # type: ignore[attr-defined]
            name=ep_name,
            input_type=input_type,
            output_type=output_type,
            method_name=fn_name,
            default=default,
            workflow_type=workflow_type,
        )
        return fn

    if func is not None:
        return decorator(func)
    return decorator


def _resolve_default_entrypoint(
    entry_points: "Mapping[str, EntryPointMetadata]",
) -> EntryPointMetadata | None:
    """Resolve the entry point to use when no ``?entrypoint=`` is provided.

    Internal helper — not part of the public SDK surface.

    Rules:
    - Exactly one entry point → that one (single-entry-point apps need no flag).
    - Multiple entry points → the single one marked ``default=True``.
    - Zero entry points, or multiple with no/ambiguous default → ``None`` (the
      caller must require an explicit entrypoint).
    """
    if len(entry_points) == 1:
        return next(iter(entry_points.values()))
    marked = [ep for ep in entry_points.values() if ep.default]
    if len(marked) == 1:
        return marked[0]
    return None


def is_entrypoint(obj: Any) -> bool:
    """Check if an object is decorated with @entrypoint."""
    return hasattr(obj, "_entrypoint_metadata")


def get_entrypoint_metadata(obj: Any) -> EntryPointMetadata | None:
    """Get entry point metadata from a decorated function."""
    return getattr(obj, "_entrypoint_metadata", None)

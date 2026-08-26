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


@dataclass(frozen=True)
class EntryPointMetadata:
    """Metadata about a registered entry point.

    Entry points are independently-triggerable execution paths on an App.
    Each entry point registers its canonical Temporal workflow type at worker
    startup; ``App.legacy_workflow_types`` may register additional inbound-only
    aliases that dispatch to the same entry point.
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
    # ``str.isalnum()`` is wider than Python's identifier grammar: characters
    # such as superscript two are alphanumeric but cannot appear in an
    # identifier. Test each code point in a non-leading position instead, since
    # the generated class always has the ``_Workflow_`` prefix.
    return "".join(char if f"x{char}".isidentifier() else "_" for char in workflow_type)


def canonical_workflow_type(app_name: str, ep: EntryPointMetadata) -> str:
    """The convention-derived Temporal workflow type for *ep*.

    ``{app-name}`` for the implicit (run()-derived) entry point, and
    ``{app-name}:{entry-point-name}`` for every explicit ``@entrypoint``.
    """
    return app_name if ep.implicit else f"{app_name}:{ep.name}"


def build_workflow_type_index(
    app_name: str,
    entry_points: Mapping[str, EntryPointMetadata],
    legacy_workflow_types: Mapping[str, str] | None = None,
) -> dict[str, EntryPointMetadata]:
    """Map every registered Temporal workflow type back to its entry point.

    The index holds each entry point's canonical convention-derived type plus
    every declared legacy alias. The worker registers one generated class per
    key, and result-type resolution reads the same index — so what is
    registered and what can be resolved cannot drift apart. SDK-initiated
    dispatch always emits the canonical type; an alias is inbound-only.

    Uniqueness is enforced on two axes. The Temporal type is the registration
    key, but dispatch under the sandbox goes through the generated class name,
    which folds both ``-`` and ``:`` to ``_``. So ``qi:bar`` and ``qi-bar`` are
    distinct types whose classes would overwrite each other in the module
    namespace, and one would silently run the other's entry point.

    Names and types must be fully disjoint so a selector is never ambiguous:
    an alias may not equal an entry-point name, and an entry-point name may
    not equal another entry point's canonical type (only reachable as an
    explicit entry point named exactly like the app while an implicit
    ``run()`` claims the bare app name).

    This function is the single validation site for the whole
    ``legacy_workflow_types`` declaration — container shape, entry shapes,
    and every collision axis. The cross-app guards in
    :func:`application_sdk.execution._temporal.workflows.get_all_app_workflows`
    are not redundant with the per-app checks here: they compare types across
    *different* apps sharing one worker, which no per-app index can see; this
    site wins only on timing, failing at class definition with a message that
    names the entry points involved.

    Args:
        app_name: The app's registered name.
        entry_points: Entry points keyed by entry-point name.
        legacy_workflow_types: ``{alias: entry-point name}`` — legacy Temporal
            workflow types external callers still dispatch, from
            ``App.legacy_workflow_types``.

    Raises:
        EntryPointContractError: If the declaration is not a mapping of
            strings, an alias restates a canonical type, targets an unknown
            entry point, shadows an entry-point name, is not a usable Temporal
            type string, an entry-point name equals a sibling's canonical
            type, or two distinct types fold to the same generated class name.
    """
    if legacy_workflow_types is None:
        legacy_workflow_types = {}
    if not isinstance(legacy_workflow_types, Mapping):
        raise EntryPointContractError(
            f"App '{app_name}': legacy_workflow_types must be a mapping of "
            f"alias strings to entry-point name strings, got "
            f"{type(legacy_workflow_types).__name__}."
        )

    index: dict[str, EntryPointMetadata] = {}
    by_class_segment: dict[str, str] = {}

    def claim_class_segment(workflow_type: str) -> None:
        segment = workflow_type_class_segment(workflow_type)
        twin = by_class_segment.get(segment)
        if twin is not None and twin != workflow_type:
            raise EntryPointContractError(
                f"App '{app_name}': Temporal workflow types '{twin}' and "
                f"'{workflow_type}' both generate the workflow class "
                f"'_Workflow_{segment}', so one would silently dispatch to "
                f"the other. Hyphens and colons both become underscores — "
                f"pick a legacy type that differs by more than those."
            )
        by_class_segment[segment] = workflow_type

    for ep in entry_points.values():
        canonical = canonical_workflow_type(app_name, ep)
        claim_class_segment(canonical)
        index[canonical] = ep

    for ep_name, ep in entry_points.items():
        claimed = index.get(ep_name)
        if claimed is not None and claimed is not ep:
            raise EntryPointContractError(
                f"App '{app_name}': entry point name '{ep_name}' equals the "
                f"canonical Temporal workflow type of entry point "
                f"'{claimed.name}', so a caller selecting '{ep_name}' would be "
                f"ambiguous. Rename the '{ep_name}' entry point."
            )

    for alias, target in legacy_workflow_types.items():
        target_ep = _validate_alias_entry(app_name, alias, target, index, entry_points)
        claim_class_segment(alias)
        index[alias] = target_ep
    return index


def _validate_alias_entry(
    app_name: str,
    alias: object,
    target: object,
    index: Mapping[str, EntryPointMetadata],
    entry_points: Mapping[str, EntryPointMetadata],
) -> EntryPointMetadata:
    """Reject a bad ``legacy_workflow_types`` entry; return its target.

    The one place that says no to an alias declaration — every rejection an
    entry can earn lives here, prefixed with the app so a multi-app worker's
    failure names its owner.
    """
    if not isinstance(alias, str) or not isinstance(target, str):
        raise EntryPointContractError(
            f"App '{app_name}': legacy_workflow_types must map alias strings "
            f"to entry-point name strings, got {alias!r}: {target!r}."
        )
    _validate_legacy_workflow_type(app_name, alias)
    claimed = index.get(alias)
    if claimed is not None:
        raise EntryPointContractError(
            f"App '{app_name}': legacy workflow type '{alias}' restates the "
            f"canonical type of entry point '{claimed.name}'. Canonical types "
            f"are always registered — declare only the legacy names that "
            f"differ from them."
        )
    if alias in entry_points:
        raise EntryPointContractError(
            f"App '{app_name}': legacy workflow type '{alias}' equals the "
            f"entry point name '{alias}'. Aliases and entry point names must "
            f"stay disjoint so a selector is never ambiguous."
        )
    target_ep = entry_points.get(target)
    if target_ep is None:
        raise EntryPointContractError(
            f"App '{app_name}': legacy workflow type '{alias}' targets "
            f"unknown entry point '{target}'. Available entry points: "
            f"{sorted(entry_points)}."
        )
    return target_ep


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


def _validate_legacy_workflow_type(app_name: str, alias: str) -> None:
    """Reject a legacy workflow type that cannot be registered.

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

    A ``:`` is deliberately accepted. The cross-worker Temporal workflow-type
    namespace is intentionally global, and a colon-qualified legacy type such
    as ``teradata-app:crawler`` is exactly the shape a migrating app must
    preserve (a colon does not make a type canonical — only the ``{app}:{ep}``
    *convention* does, and an alias exists precisely to sit outside it).
    Same-worker collisions are rejected at startup by
    :func:`build_workflow_type_index`; cross-worker duplication is a deployment
    concern, not something a per-app validator can or should police.
    """
    if not alias:
        raise EntryPointContractError(
            f"App '{app_name}': legacy_workflow_types aliases must be "
            f"non-empty strings."
        )
    if any(char.isspace() or not char.isprintable() for char in alias):
        raise EntryPointContractError(
            f"App '{app_name}': legacy_workflow_types alias must not contain "
            f"whitespace or control characters, got {alias!r}."
        )
    if not any(char.isalnum() for char in alias):
        raise EntryPointContractError(
            f"App '{app_name}': legacy_workflow_types alias {alias!r} is not "
            f"a usable Temporal workflow type — it carries no letters or "
            f"digits."
        )


def entrypoint(
    func: F | None = None,
    *,
    name: str | None = None,
    default: bool = False,
) -> F | Callable[[F], F]:
    """Decorator to mark a method as an independently-triggerable entry point.

    Each entry point registers its canonical Temporal workflow type at worker
    startup. Multiple entry points on the same App share @task methods as
    activities.

    Entry points are triggered via HTTP POST /workflows/v1/start?entrypoint=<name>.
    The body field 'workflow_type' is also accepted as a transitional fallback.

    Workflow naming:
    - An implicit ``run()`` entry point: ``{app-name}``.
    - An explicit ``@entrypoint``: ``{app-name}:{entry-point-name}``.
    - A legacy type external callers still dispatch is declared on the App
      class as an inbound-only alias — see ``App.legacy_workflow_types``.

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
        input_type, output_type = _validate_entrypoint_signature(fn)
        fn._entrypoint_metadata = EntryPointMetadata(  # type: ignore[attr-defined]
            name=ep_name,
            input_type=input_type,
            output_type=output_type,
            method_name=fn_name,
            default=default,
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

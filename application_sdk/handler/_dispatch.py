"""Resolve which callable answers a handler op for an entry point.

One resolution for every surface that runs a handler op: the HTTP routes
(``application_sdk.handler.routes``, shared by the worker's handler service and
the consolidated API host) and the SDR Temporal activities
(``application_sdk.execution._temporal.sdr``). Before this module the SDR
activities called ``Handler.test_auth`` / ``Handler.fetch_metadata`` directly, so
a per-entrypoint ``app.<segment>.handler`` hook ran over HTTP and was silently
skipped over SDR.

This module is part of the api slice's core: it must not import Temporal or any
worker-only module, so it ships in ``atlan-application-sdk-api``.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any, Literal, cast, overload

from application_sdk._logging import get_logger
from application_sdk.errors.leaves import InvalidInputError
from application_sdk.handler.base import Handler
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    WarmupInput,
    WarmupObservation,
)

# Named for application_sdk.handler.service: discovery moved from there (via
# routes.py) and keeps its logger name, so its debug line is unchanged.
logger = get_logger("application_sdk.handler.service")

# ``\Z``, not ``$``: ``$`` also matches before a trailing newline, so
# ``"export\n"`` would pass and then miss discovery instead of being refused.
_ENTRYPOINT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*\Z")

HandlerFn = Callable[[Any, HandlerContext], Awaitable[Any]]

HandlerOp = Literal["test_auth", "preflight_check", "fetch_metadata", "warmup"]


def entrypoint_module_segment(name: str) -> str:
    """Kebab-case entry-point name → its Python module segment (``a-b`` → ``a_b``).

    Same conversion as ``application_sdk.app.entrypoint.entrypoint_module_segment``.
    """
    return name.replace("-", "_")


def _import_optional_app_module(dotted: str) -> ModuleType | None:
    """Import an optional consumer-owned module by dotted path.

    Returns ``None`` only when the module *itself* (or one of its parent
    packages) is absent — the expected "this app ships no such hook" case.
    Re-raises when the module exists but fails to import: a transitive
    ``ModuleNotFoundError`` (a missing dependency named somewhere other than the
    target's own dotted path) or any other ``ImportError`` is a real bug in the
    connector's code and must surface, not be silently swallowed into a
    fall-through.
    """
    import importlib  # noqa: PLC0415 — cold path: per-entrypoint discovery only

    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:  # conformance: ignore[E008] re-raises if missing dep is not the target module itself
        missing = exc.name or ""
        # Swallow only if what's missing is the target or one of its parents
        # (e.g. ``app``, ``app.<segment>``, ``app.<segment>.core``).
        if missing == dotted or dotted.startswith(f"{missing}."):
            return None
        raise


def _discover_handler_fn(
    entrypoint: str, app_package: str, fn_name: str
) -> HandlerFn | None:
    """Look for a per-entrypoint handler function.

    Convention: ``app.<segment>.handler.<fn_name>`` where ``segment`` is
    :func:`~application_sdk.app.entrypoint.entrypoint_module_segment` of the
    entry-point name and ``fn_name`` is one of ``"test_auth"``,
    ``"preflight_check"``, ``"fetch_metadata"``, ``"warmup"``. Multi-entrypoint apps that
    need *per-entrypoint* lifecycle hooks drop a ``handler.py`` next to their
    package's hand-written code with::

        async def test_auth(input: AuthInput, ctx: HandlerContext) -> AuthOutput: ...
        async def preflight_check(input: PreflightInput, ctx: HandlerContext) -> PreflightOutput: ...
        async def fetch_metadata(input: MetadataInput, ctx: HandlerContext) -> MetadataOutput: ...
        async def warmup(input: WarmupInput, ctx: HandlerContext) -> WarmupObservation: ...

    The dispatch is best-effort: if the per-entrypoint module / attribute
    is absent, the caller falls through to the app-level ``Handler`` instance
    (``DefaultHandler`` if no custom handler is configured), preserving
    today's single-entrypoint behavior 1:1.

    Precedence & silent fall-through — important when reasoning about which
    code actually runs (mirrored in ``docs/concepts/handlers.md``):

    - When a per-entrypoint ``<fn_name>`` exists, it **pre-empts** the
      app-level ``Handler.<fn_name>`` for that entry point. Defining both
      silently runs the module one; the class method never executes.
    - Resolution is per-op: a module that defines only ``fetch_metadata``
      leaves ``test_auth`` / ``preflight_check`` falling back to the
      app-level ``Handler`` — one entry point can be split across two files.
    - A wrong name or a non-``async`` ``def`` does not match (see the
      ``iscoroutinefunction`` check below) and **silently** falls through
      rather than erroring — so a typo'd hook quietly does nothing.

    Returns the callable or ``None`` if absent.
    """

    segment = entrypoint_module_segment(entrypoint)
    module = _import_optional_app_module(f"{app_package}.{segment}.handler")
    if module is None:
        return None
    fn = getattr(module, fn_name, None)
    # The dispatch ``await``s the result, so require a coroutine function — a
    # sync ``def`` falls through to the app-level Handler rather than blowing up
    # with a TypeError at request time.
    if fn is not None and not inspect.iscoroutinefunction(fn):
        logger.debug(
            "%s.%s.%s found but not async; falling through to app-level Handler",
            app_package,
            segment,
            fn_name,
        )
    return cast("HandlerFn | None", fn if inspect.iscoroutinefunction(fn) else None)


@overload
def resolve_handler_op(
    handler: Handler,
    op: Literal["test_auth"],
    entrypoint: str,
    *,
    app_package: str = ...,
) -> Callable[[AuthInput, HandlerContext], Awaitable[AuthOutput]]: ...
@overload
def resolve_handler_op(
    handler: Handler,
    op: Literal["preflight_check"],
    entrypoint: str,
    *,
    app_package: str = ...,
) -> Callable[[PreflightInput, HandlerContext], Awaitable[PreflightOutput]]: ...
@overload
def resolve_handler_op(
    handler: Handler,
    op: Literal["fetch_metadata"],
    entrypoint: str,
    *,
    app_package: str = ...,
) -> Callable[[MetadataInput, HandlerContext], Awaitable[MetadataOutput]]: ...
@overload
def resolve_handler_op(
    handler: Handler,
    op: Literal["warmup"],
    entrypoint: str,
    *,
    app_package: str = ...,
) -> Callable[[WarmupInput, HandlerContext], Awaitable[WarmupObservation]]: ...
def resolve_handler_op(
    handler: Handler,
    op: HandlerOp,
    entrypoint: str,
    *,
    app_package: str = "app",
) -> HandlerFn:
    """Return the callable that answers ``op`` for ``entrypoint``.

    A per-entrypoint ``<app_package>.<segment>.handler.<op>`` hook wins (see
    :func:`_discover_handler_fn` for the precedence rules); otherwise the
    app-level ``handler.<op>`` answers. Both come back with the hook signature
    ``(input, ctx)`` so callers invoke them the same way; the ``Handler`` method
    ignores ``ctx`` (it reads ``self.context``, bound by the caller).

    An empty ``entrypoint`` (single-entrypoint apps, older orchestrators) goes
    straight to ``handler``.

    Raises:
        InvalidInputError: ``entrypoint`` is non-empty and not a valid entry-point
            name. The name becomes part of a module path, so a malformed one is
            refused rather than imported. The HTTP routes reject it with a 400
            before calling this.
    """
    if entrypoint:
        if not _ENTRYPOINT_NAME_RE.match(entrypoint):
            raise InvalidInputError(
                message="Invalid entrypoint name",
                field="entrypoint",
                constraint=_ENTRYPOINT_NAME_RE.pattern,
            )
        fn = _discover_handler_fn(entrypoint, app_package, op)
        if fn is not None:
            return fn
    method: Callable[[Any], Awaitable[Any]] = getattr(handler, op)

    async def call_handler(input: Any, ctx: HandlerContext) -> Any:
        return await method(input)

    return call_handler


def mirror_template_key(metadata_input: MetadataInput) -> MetadataInput:
    """Copy the widget routing key onto ``object_filter`` when that is empty.

    The widget routing key (``metadataTemplateKey`` / ``type`` on the wire) lands
    in its documented home, ``metadata_template_key``, via the field's
    validation alias. Per-entrypoint hooks that read the legacy ``object_filter``
    (e.g. asset-export-advanced's tags vs connectors vs typenames widgets) keep
    working because it is mirrored there. New hooks can read
    ``metadata_template_key`` directly. Returns a copy; the input is not mutated.
    """
    if not metadata_input.object_filter and metadata_input.metadata_template_key:
        return metadata_input.model_copy(
            update={"object_filter": metadata_input.metadata_template_key}
        )
    return metadata_input

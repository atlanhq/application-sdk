"""Generate the Temporal workflow and activity glue for an ``App``.

``App`` stays free of the execution runtime: this module turns an App
subclass's entry points into ``@workflow.defn`` classes, and its ``@task``
methods into wrappers that dispatch Temporal activities. It imports
``application_sdk.app``; nothing in ``application_sdk.app`` imports it back.
The sandbox rebuild that ``App.__init_subclass__`` triggers reaches it through
the installer this module registers on import (``_install_workflow_classes``).
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    TypedDict,
    TypeVar,
    cast,
    get_type_hints,
)

from pydantic import BaseModel
from temporalio import workflow
from temporalio.exceptions import ActivityError, FailureError
from temporalio.exceptions import ApplicationError as _TemporalApplicationError

from application_sdk.app import base as _app_base
from application_sdk.app.base import (
    _BUILD_IDENTITY,
    App,
    RetryableError,
    _run_preflight_gate,
    _RunStoredRefs,
    _safe_log,
    _safe_now,
)
from application_sdk.app.context import (
    AppContext,
)
from application_sdk.app.entrypoint import (
    EntryPointMetadata,
    canonical_workflow_type,
    workflow_type_class_segment,
)
from application_sdk.app.registry import resolve_pool_queue
from application_sdk.app.task import get_task_metadata, is_task
from application_sdk.constants import (
    LOCAL_WORKFLOW_ID,
)
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.fetch_failures import (
    MergeFetchFailuresInput,
)
from application_sdk.contracts.skipped_records import (
    MergeSkippedRecordsInput,
)
from application_sdk.contracts.types import FileReference, StoreTarget
from application_sdk.errors.base import AppError as _NewAppError
from application_sdk.errors.leaves import WORKER_EVICTED_TYPE
from application_sdk.errors.leaves import InvalidInputError as _InvalidInputError
from application_sdk.execution._temporal.activities import TaskContext
from application_sdk.execution._temporal.eviction_retry import (
    execute_activity_with_eviction_retry,
)
from application_sdk.execution._temporal.preflight_gate import (
    input_type_supports_gate,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.execution.retry import RetryPolicy as _RP
from application_sdk.execution.retry import (
    _to_temporal_retry_policy,
    resolve_activity_time_bounds,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs import Metric, get_outputs
from application_sdk.outputs.fetch_failures import (
    FETCH_FAILURES_METRIC,
    FETCH_FAILURES_UNPERSISTED_METRIC,
)
from application_sdk.outputs.skipped import (
    SKIPPED_RECORDS_METRIC,
    SKIPPED_RECORDS_UNPERSISTED_METRIC,
)
from application_sdk.storage.file_ref_sync import _find_file_refs

if TYPE_CHECKING:
    from application_sdk.execution.progress import ProgressWatchdogMode


_task_logger = get_logger(__name__)


def _run_started_at_epoch() -> float:
    """When this run started, in wall-clock seconds since the epoch.

    Deterministic for replay: ``workflow.info().start_time`` comes from the
    run's own history, so every replay of a run reads the same instant and the
    run-length observation measures the run rather than the attempt that
    happens to be replaying it.

    Returns ``0.0`` outside a workflow context — a task invoked directly in a
    test or a local run has no run to measure. Never raises: this value only
    feeds an alert, so failing to read it must cost the observation and nothing
    else.
    """
    try:
        start_time = workflow.info().start_time
    except Exception:  # noqa: BLE001 — probe for Temporal workflow context; the value only feeds an observability signal, so any failure degrades to "unknown run start"
        return 0.0  # conformance: ignore[E007] workflow-context probe; "no run to measure" is the answer, and a log line here would fire per dispatch on every local run
    return start_time.timestamp() if start_time is not None else 0.0


# Cache generated workflow classes keyed by (app_cls, entry_point_name,
# workflow_type) so generate_workflow_class() is idempotent across repeated
# calls (e.g. tests or worker re-creation) and never registers the same Temporal
# workflow twice.
_workflow_class_cache: dict[tuple[type, str, str], type] = {}

_EARLY_RUN_BINDING_PATCH = "atlan-sdk/early-run-binding"
"""``workflow.patched`` marker recorded by every run whose generated
``__init__`` binds the App instance to the run (FND-5219). A history without it
was written before that change and binds in ``_run``, as it did then."""


def _validate_interaction_signature(
    fn: Callable[..., Any],
    kind: Literal["signal", "query", "update"],
    fn_name: str,
) -> None:
    """Validate that a @signal / @query / @update method satisfies the interaction contract.

    Rules enforced at class-definition time:
    - ``@signal``: no params besides ``self`` (pure trigger, no payload).
    - ``@query``: no params besides ``self``; return type must be a subclass of Output.
    - ``@update``: exactly one param besides ``self`` that is a subclass of Input;
      return type must be a subclass of Output.

    Dynamic interactions (``name is None``) are skipped — callers must check before
    calling this function.

    Args:
        fn: The original (undecorated) interaction function.
        kind: One of ``"signal"``, ``"query"``, or ``"update"``.
        fn_name: Human-readable name used in error messages.

    Raises:
        _InvalidInputError: If the signature does not satisfy the contract.
    """
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "self"]

    try:
        hints: dict[str, Any] = get_type_hints(fn)
    # conformance: ignore[E004,E009] get_type_hints probe; broad catch intentional — falls back to __annotations__ when forward refs are unresolvable
    except Exception:  # noqa: BLE001 — get_type_hints probe; falls back to __annotations__ (see directive above)
        hints = getattr(fn, "__annotations__", {})

    if kind == "signal":
        if params:
            raise _InvalidInputError(
                message=(
                    f"@signal '{fn_name}' must have no parameters besides self "
                    f"(signals are pure triggers — they carry no payload). "
                    f"Got {len(params)} extra parameter(s): "
                    f"{[p.name for p in params]}. "
                    f"To carry data into a running workflow, use @update instead."
                )
            )

    elif kind == "query":
        if params:
            raise _InvalidInputError(
                message=(
                    f"@query '{fn_name}' must have no parameters besides self. "
                    f"Got {len(params)} extra parameter(s): "
                    f"{[p.name for p in params]}. "
                    f"Queries are read-only probes; pass context via instance fields set "
                    f"by an earlier @update if needed."
                )
            )
        return_type = hints.get("return")
        if not (
            return_type is not None
            and isinstance(return_type, type)
            and issubclass(return_type, Output)
        ):
            raise _InvalidInputError(
                message=(
                    f"@query '{fn_name}' return type must be a subclass of Output, "
                    f"got {return_type!r}. "
                    f"Define a dataclass that extends Output and annotate the return type."
                )
            )

    else:  # kind == "update"
        if len(params) != 1:
            raise _InvalidInputError(
                message=(
                    f"@update '{fn_name}' must have exactly one parameter besides self "
                    f"(a subclass of Input), got {len(params)}. "
                    f"Wrap multiple values in a single Input dataclass."
                )
            )
        param = params[0]
        input_type = hints.get(param.name)
        if not (
            input_type is not None
            and isinstance(input_type, type)
            and issubclass(input_type, Input)
        ):
            raise _InvalidInputError(
                message=(
                    f"@update '{fn_name}' parameter '{param.name}' must be a subclass "
                    f"of Input, got {input_type!r}. "
                    f"Define a dataclass that extends Input and use it as the parameter type."
                )
            )
        return_type = hints.get("return")
        if not (
            return_type is not None
            and isinstance(return_type, type)
            and issubclass(return_type, Output)
        ):
            raise _InvalidInputError(
                message=(
                    f"@update '{fn_name}' return type must be a subclass of Output, "
                    f"got {return_type!r}. "
                    f"Define a dataclass that extends Output and annotate the return type."
                )
            )


class _GeneratedWorkflow(Protocol):
    """What a generated workflow instance carries: the run's App instance."""

    _app_instance: App
    _run_context_data: dict[str, Any]


def _build_relay(
    app_cls: type[App],
    cls_name: str,
    method_name: str,
    is_coroutine: bool,
    *,
    external_payload: bool = False,
) -> Callable[..., object]:
    """Construct a wf_cls-level method that delegates to self._app_instance.

    ``external_payload`` marks an ``@update``: its argument comes from
    outside the run, like a start payload, so any ``store=UPSTREAM`` ref in
    it is demoted before app code sees it (see ``_demote_upstream_refs``).
    """
    if is_coroutine:

        async def _async_relay(
            wf_self: _GeneratedWorkflow, *args: object, **kwargs: object
        ) -> object:
            bound = getattr(wf_self._app_instance, method_name)
            if external_payload:
                args, kwargs = _demote_args(args, kwargs)
            return await bound(*args, **kwargs)

        _async_relay.__name__ = method_name
        _async_relay.__qualname__ = f"{cls_name}.{method_name}"
        _async_relay.__module__ = app_cls.__module__
        return _async_relay

    def _sync_relay(
        wf_self: _GeneratedWorkflow, *args: object, **kwargs: object
    ) -> object:
        bound = getattr(wf_self._app_instance, method_name)
        if external_payload:
            args, kwargs = _demote_args(args, kwargs)
        return bound(*args, **kwargs)

    _sync_relay.__name__ = method_name
    _sync_relay.__qualname__ = f"{cls_name}.{method_name}"
    _sync_relay.__module__ = app_cls.__module__
    return _sync_relay


def _build_validator_relay(
    orig_validator: Callable[..., object],
) -> Callable[..., object]:
    def vrelay(wf_self: _GeneratedWorkflow, *args: object, **kwargs: object) -> object:
        return orig_validator(wf_self._app_instance, *args, **kwargs)

    return vrelay


def _relay_for(
    app_cls: type[App], cls_name: str, member_name: str, member: object
) -> Callable[..., object] | None:
    """The relay for one App member, or ``None`` if it is not an interaction."""
    signal_defn = getattr(member, "__temporal_signal_definition", None)
    query_defn = getattr(member, "__temporal_query_definition", None)
    update_defn = getattr(member, "_defn", None)

    # @workflow.update returns a callable with both `_defn` and `validator`
    # (Temporal's runtime_checkable UpdateMethodMultiParam Protocol). The
    # `validator` attribute distinguishes it from arbitrary objects that
    # might happen to carry a `_defn` field.
    is_update = update_defn is not None and hasattr(member, "validator")

    if not (signal_defn or query_defn or is_update):
        return None

    # Contract enforcement — skip dynamic interactions (name is None).
    if signal_defn is not None and signal_defn.name is not None:
        _validate_interaction_signature(signal_defn.fn, "signal", member_name)
    elif query_defn is not None and query_defn.name is not None:
        _validate_interaction_signature(query_defn.fn, "query", member_name)
    elif is_update and update_defn is not None and update_defn.name is not None:
        _validate_interaction_signature(update_defn.fn, "update", member_name)

    relay = _build_relay(
        app_cls,
        cls_name,
        member_name,
        inspect.iscoroutinefunction(member),
        external_payload=is_update,
    )

    if signal_defn is not None:
        # Rebind the definition's fn to the relay so Temporal's _bind_method
        # passes wf_self (not an App instance) as the first arg.
        relay.__temporal_signal_definition = replace(  # pyright: ignore[reportFunctionMemberAccess]
            signal_defn, fn=relay
        )
    elif query_defn is not None:
        relay.__temporal_query_definition = replace(  # pyright: ignore[reportFunctionMemberAccess]
            query_defn, fn=relay
        )
    else:
        # update_defn is _UpdateDefinition (asserted above by `is_update`).
        assert update_defn is not None
        new_validator: Callable[..., object] | None = None
        if update_defn.validator is not None:
            new_validator = _build_validator_relay(update_defn.validator)

        relay._defn = replace(  # pyright: ignore[reportFunctionMemberAccess]
            update_defn, fn=relay, validator=new_validator
        )
        # Temporal's @workflow.update decorator also sets a `.validator`
        # attribute on the decorated fn (partial(_update_validator, defn)).
        # We don't need it for runtime dispatch — Temporal reads the
        # validator off the definition — but provide one so the relay
        # structurally matches UpdateMethodMultiParam.
        relay.validator = lambda fn: fn  # pyright: ignore[reportFunctionMemberAccess]
    return relay


def _collect_interaction_relays(
    app_cls: "type[App]", cls_name: str
) -> dict[str, Callable[..., object]]:
    """Scan the App class for @signal / @query / @update runtime interactions and
    synthesize per-interaction relay methods bound to the generated wf_cls.

    Each relay extracts the per-run App instance from ``wf_self._app_instance`` and
    delegates the call. The synthesized relay carries Temporal's discovery metadata
    (rebound to point at the relay), so @workflow.defn(wf_cls) registers the
    interaction against the generated class — which is what Temporal requires.

    Returns a mapping of method name -> relay callable, ready to be placed on wf_cls.
    """
    relays: dict[str, Callable[..., object]] = {}
    for member_name, member in inspect.getmembers(app_cls):
        if member_name == "run":
            # The entry method is handled separately; never relay it as an interaction.
            continue
        relay = _relay_for(app_cls, cls_name, member_name, member)
        if relay is not None:
            relays[member_name] = relay
    return relays


async def _merge_skipped_records(app_instance: "App") -> None:
    """Schedule ``merge_skipped_records`` when the run skipped anything (FND-4881).

    Workflow code. Reads the run's summed skip metrics from the workflow's
    output collector, which already holds every completed activity's carried
    outputs. A run with no skips schedules nothing, so its history — and the
    replay of any run started before this path existed — is unchanged.
    Fails open: a merge failure is logged and every skip not already counted
    as unpersisted is recorded as ``skipped-records-unpersisted``.
    """
    outputs = get_outputs()
    skipped = int(outputs.numeric_metric(SKIPPED_RECORDS_METRIC))
    if skipped <= 0:
        return
    unpersisted = int(outputs.numeric_metric(SKIPPED_RECORDS_UNPERSISTED_METRIC))
    try:
        await app_instance.merge_skipped_records(
            MergeSkippedRecordsInput(
                skipped_count=skipped, unpersisted_count=unpersisted
            )
        )
    except Exception:  # noqa: BLE001 — fail-open by design (FND-4881): logged via _safe_log with exc_info=True and counted in skipped-records-unpersisted
        _safe_log(
            "error",
            "Failed to merge skipped-record parts; the run has no "
            "skipped-records artifact (parts remain under the run prefix)",
            skipped_count=skipped,
            exc_info=True,
        )
        lost = skipped - unpersisted
        if lost > 0:
            outputs.add_metric(
                Metric(name=SKIPPED_RECORDS_UNPERSISTED_METRIC, value=lost)
            )


async def _merge_fetch_failures(app_instance: "App") -> None:
    """Schedule ``merge_fetch_failures`` when the run recorded any (FND-5294).

    Workflow code, the sibling of :func:`_merge_skipped_records`. A run with
    no fetch failures schedules nothing, and ``workflow.patched`` keeps any
    run started before this path existed replaying unchanged, even one whose
    app emitted a ``fetch-failures`` metric of its own. Fails open: a
    merge failure is logged and every failure not already counted as
    unpersisted is recorded as ``fetch-failures-unpersisted``.
    """
    outputs = get_outputs()
    failures = int(outputs.numeric_metric(FETCH_FAILURES_METRIC))
    if failures <= 0:
        return
    # An app on an older SDK may have emitted its own ``fetch-failures``
    # metric. Replaying that history must not gain the new activity, so the
    # command is versioned rather than relying on the name being unused.
    if not workflow.patched("fetch-failures-merge"):
        return
    unpersisted = int(outputs.numeric_metric(FETCH_FAILURES_UNPERSISTED_METRIC))
    try:
        await app_instance.merge_fetch_failures(
            MergeFetchFailuresInput(
                failure_count=failures, unpersisted_count=unpersisted
            )
        )
    except Exception:  # noqa: BLE001 — fail-open by design (FND-5294): logged via _safe_log with exc_info=True and counted in fetch-failures-unpersisted
        _safe_log(
            "error",
            "Failed to merge fetch-failure parts; the run has no "
            "fetch-failures artifact (parts remain under the run prefix)",
            fetch_failure_count=failures,
            exc_info=True,
        )
        lost = failures - unpersisted
        if lost > 0:
            outputs.add_metric(
                Metric(name=FETCH_FAILURES_UNPERSISTED_METRIC, value=lost)
            )


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _demote_upstream_refs(model: _ModelT) -> _ModelT:
    """Reset ``store=UPSTREAM`` to ``DEPLOYMENT`` on every ref in an external payload.

    Applied at both places a payload enters a run from outside it: the start
    input (``_validate_workflow_input``, which also covers ``continue_with``)
    and every ``@update`` argument (the interaction relay). Signals and queries
    carry no payload.

    ``FileReference.store`` decides which bucket the interceptor reads a ref
    from, so it is only trusted when this run set it: ``App.download`` /
    ``App.upload`` tag the refs they return. A workflow's start payload comes
    from outside the run, and honouring the tag there would let whoever
    starts the workflow aim a materialise at any key in the upstream bucket
    (FND-3795). Demoting restores exactly the pre-tag behaviour for those refs:
    they are read from the deployment store, as every ref was before.
    """
    return cast("_ModelT", _demote(model))


def _demote_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Apply :func:`_demote` to an ``@update`` relay's call arguments."""
    return tuple(_demote(a) for a in args), {k: _demote(v) for k, v in kwargs.items()}


_REF_DUMP_KEYS = frozenset({"storage_path", "is_durable", "store"})


def _log_demoted(storage_path: object) -> None:
    _safe_log(
        "warning",
        "Ignoring store=upstream on a FileReference in the workflow input; "
        "only refs App.download/App.upload return in this run read the "
        "upstream store",
        storage_path=storage_path,
    )


def _demote(node: object) -> object:
    """Tree walk for :func:`_demote_upstream_refs`; returns *node* itself if unchanged.

    Reaches every place a ref can ride in a payload: typed fields, model
    extras (an ``extra="allow"`` input carrying app state), and plain dicts
    shaped like a dumped ``FileReference``. That last one is how an app
    round-trips refs through untyped state (``model_dump()`` out,
    ``FileReference(**raw)`` back in after ``continue_with``); left alone, the
    rebuilt ref would carry a tag nobody in this run set.
    """
    if isinstance(node, FileReference):
        if node.store is not StoreTarget.UPSTREAM:
            return node
        _log_demoted(node.storage_path)
        return node.model_copy(update={"store": StoreTarget.DEPLOYMENT})
    if isinstance(node, BaseModel):
        changes: dict[str, object] = {}
        for name in type(node).model_fields:
            old = getattr(node, name)
            new = _demote(old)
            if new is not old:
                changes[name] = new
        for name, old in (node.model_extra or {}).items():
            new = _demote(old)
            if new is not old:
                changes[name] = new
        return node.model_copy(update=changes) if changes else node
    if isinstance(node, (list, tuple)):
        items = [_demote(v) for v in node]
        if all(a is b for a, b in zip(items, node)):
            return node
        return items if isinstance(node, list) else tuple(items)
    if isinstance(node, dict):
        mapped = {k: _demote(v) for k, v in node.items()}
        if _REF_DUMP_KEYS <= mapped.keys() and mapped["store"] == StoreTarget.UPSTREAM:
            _log_demoted(mapped.get("storage_path"))
            mapped["store"] = StoreTarget.DEPLOYMENT.value
        if all(mapped[k] is node[k] for k in node):
            return node
        return mapped
    return node


def _validate_workflow_input(raw_input: object, input_type: type[Input]) -> Input:
    """Validate a decoded workflow payload against the entry point's typed contract.

    The generated workflow declares its argument to Temporal with a permissive
    ``Any`` annotation (see ``generate_workflow_class``). That is deliberate: if the
    argument were annotated as ``input_type``, Temporal's pydantic data converter
    would validate the payload while *decoding the workflow task*, and a bad payload
    would surface as a Workflow *Task* failure — which Temporal retries with backoff
    **indefinitely** (there is no max-attempts for workflow tasks). A permanently
    invalid input (e.g. a required object field sent as ``""``) would then retry
    forever instead of failing.

    Validating here, inside the workflow body, converts that into a clean,
    non-retryable workflow *execution* failure that fails fast.

    This does **not** loosen or bypass the typed contract. The payload is validated
    against exactly ``input_type`` — the same model the entry point declares — before
    any app code runs, and the fully typed, validated model is what every downstream
    caller (preflight gate, ``_log_summary``, the entry point method) receives. The
    permissive annotation is confined to this generated wire boundary; there is no
    path by which a raw payload reaches app code, and app authors cannot opt into it.

    Note that this validates in pydantic's python/lax mode (``model_validate`` on a
    decoded dict) rather than the json mode the removed typed annotation gave Temporal's
    data converter. The two are equivalent for every field type a permitted ``Input``
    tree allows — ``bytes``/``bytearray``, the one genuinely mode-divergent type, is
    forbidden by ``validate_payload_safety`` (contracts/base.py). A maintainer adding a
    custom, mode-sensitive pydantic type to a contract should re-check that equivalence.

    Args:
        raw_input: The decoded payload — a dict from the wire, or (unit tests / the
            in-process ``/start`` path) an already-constructed ``input_type``.
        input_type: The entry point's typed input contract.

    Returns:
        A validated ``input_type`` instance.

    Raises:
        ApplicationError: non-retryable, if the payload does not satisfy ``input_type``.
    """
    if isinstance(raw_input, input_type):
        # Already the typed model (constructed by the SDK /start path or a test);
        # it was validated at construction, so re-validation would be redundant.
        return _demote_upstream_refs(raw_input)

    try:
        return _demote_upstream_refs(input_type.model_validate(raw_input))
    # conformance: ignore[E004] deterministic input-validation failure; logged via _safe_log with exc_info=True and re-raised as a non-retryable ApplicationError
    except Exception as e:
        _safe_log(
            "error",
            "Workflow input failed validation against its typed contract",
            input_type=input_type.__name__,
            exc_info=True,
        )
        # Non-retryable: a payload that fails schema validation is deterministic —
        # retrying the same input can never succeed. Failing the execution now
        # avoids the indefinite Workflow Task retry loop that a decode-time failure
        # would cause.
        raise ApplicationError(
            f"Invalid workflow input for {input_type.__name__}: {e}",
            type="InputValidationError",
            non_retryable=True,
        ) from e


def _lift_non_retryable_activity_failure(
    exc: BaseException,
) -> _TemporalApplicationError | None:
    """The non-retryable failure a task raised, lifted to the workflow's top level.

    A ``@task`` that raises a non-retryable error reaches the workflow as
    ``ActivityError(cause=ApplicationError(non_retryable=True))``. Temporal reads
    ``non_retryable`` only on the *top-level* failure of a workflow execution, so
    re-raising the ``ActivityError`` unchanged lets the workflow-level
    ``RetryPolicy`` re-run the whole workflow, which fails the same way each
    time. Lifting the cause's type, message and ``details`` onto a top-level
    non-retryable ``ApplicationError`` ends the run after one attempt; the
    caller raises it ``from`` the ``ActivityError``, so the original chain (and
    ``details[0]`` on the cause) is still there for anything that walks it.

    ``WorkerEvicted`` is excluded: it is non-retryable only so the eviction
    re-dispatch loop sees it at once. An eviction that outlasts that loop is
    infrastructure, not a deterministic failure, so a workflow retry may still
    rescue it.

    Returns:
        The lifted error, or ``None`` when ``exc`` is not an ``ActivityError``
        whose direct cause is a non-retryable, non-eviction ``ApplicationError``.
    """
    if not isinstance(exc, ActivityError):
        return None
    cause = exc.cause
    if not isinstance(cause, _TemporalApplicationError) or not cause.non_retryable:
        return None
    if cause.type == WORKER_EVICTED_TYPE:
        return None
    return ApplicationError(
        cause.message,
        *cause.details,
        type=cause.type,
        non_retryable=True,
    )


def _warn_if_gate_cannot_run(
    input_type: type[Input], entrypoint_name: str, workflow_name: str
) -> None:
    """Boot-time advice: the preflight gate cannot run for this entry point."""
    if workflow.unsafe.in_sandbox() or input_type_supports_gate(input_type):
        return
    _task_logger.warning(
        "Preflight gate will not run for entrypoint '%s' (%s): input type %s does "
        "not declare the credential-routing fields (extraction_method, "
        "credential_guid, agent_json) top-level. Expected for source-less "
        "entrypoints; if this entrypoint verifies a data source, declare those "
        "fields (e.g. via the contract toolkit's ExtractionInput) so source "
        "verification runs before extraction.",
        entrypoint_name,
        workflow_name,
        input_type.__name__,
    )


def _bind_app_instance_to_run(
    app_cls: type[App], app_instance: App, started_at: datetime
) -> dict[str, Any]:
    """Give ``app_instance`` this run's context, stored refs and task wrappers.

    Returns the context dict the task wrappers hold. Its ``correlation_id``
    starts as the run id; ``_run`` sets the resolved one, which needs the
    start input.
    """
    info = workflow.info()
    context_data: dict[str, Any] = {
        "run_id": info.run_id,
        "workflow_id": info.workflow_id,
        "correlation_id": info.run_id,
    }
    app_instance._context = AppContext(
        app_name=app_cls._app_name,
        app_version=app_cls._app_version,
        build_id=app_cls._app_build_id,
        run_id=info.run_id,
        workflow_id=info.workflow_id,
        correlation_id=info.run_id,
        started_at=started_at,
    )
    app_instance._run_stored_refs = _RunStoredRefs()
    # BLDX-878: inter-app calls deactivated pending review.
    # app_instance._client = WorkflowAppClient(context_data)
    _wrap_instance_tasks(app_instance, context_data)
    return context_data


def _instance_for_run(
    wf_self: _GeneratedWorkflow, app_cls: type[App], start_time: datetime
) -> tuple[App, dict[str, Any]]:
    """The run's App instance and the context dict its task wrappers hold.

    ``wf_cls.__init__`` builds the App instance and binds it to the run
    (context, stored refs, @task wrapping) before Temporal applies any
    @signal / @update delivered in the run's first activation, which runs
    before ``_run`` starts. See ``_bind_app_instance_to_run``. Bind here
    instead when ``wf_self`` is a stand-in (e.g. MagicMock from unit tests)
    where ``__init__`` didn't run, or a replay of a run started before
    ``__init__`` bound it.
    """
    existing = getattr(wf_self, "_app_instance", None)
    context_data = getattr(wf_self, "_run_context_data", None)
    if isinstance(existing, app_cls) and isinstance(context_data, dict):
        return existing, cast("dict[str, Any]", context_data)
    app_instance = existing if isinstance(existing, app_cls) else app_cls()
    wf_self._app_instance = app_instance
    return app_instance, _bind_app_instance_to_run(app_cls, app_instance, start_time)


def _correlation_id_for(run_id: str) -> str:
    """The run's correlation id, or its run id when none can be read."""
    try:
        with workflow.unsafe.imports_passed_through():
            from application_sdk.observability.correlation import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
                get_correlation_context,
            )

        _corr_ctx = get_correlation_context()
        return _corr_ctx.correlation_id if _corr_ctx else run_id
    except Exception:  # noqa: BLE001 — logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
        _safe_log(
            "warning",
            "Failed to read correlation context, falling back to run_id",
            exc_info=True,
        )
        return run_id


def _log_input_summary(input_data: Input, context: AppContext) -> None:
    """Log that the run has an input summary; never raises."""
    try:
        if hasattr(input_data, "_log_summary"):
            # The summary itself is not attached: ``_log_summary`` drops
            # fields by name only, so a value such as a URL carrying
            # userinfo would pass through unredacted to OTLP and the
            # object-store log sink. It was never exported before either.
            input_summary = input_data._log_summary()
            if input_summary:
                _safe_log(
                    "info",
                    "App input",
                    app_name=context.app_name,
                    run_id=str(context.run_id),
                    correlation_id=context.correlation_id,
                )
    except Exception:  # noqa: BLE001 — logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
        _safe_log("warning", "Failed to log input summary", exc_info=True)


def _log_run_failure(e: Exception, context: AppContext) -> None:
    """Log why the run failed: terse for a preflight block, in full otherwise."""
    with workflow.unsafe.imports_passed_through():
        from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            is_preflight_block,
            preflight_block_message,
        )
    # A deliberate preflight-gate block logs terse (classification already
    # on the error's FailureDetails); the marker may sit on a cause, so
    # the reason is read off the block itself, not Temporal's wrapper.
    if is_preflight_block(e):
        _safe_log(
            "warning",
            "App blocked by preflight gate",
            app_name=context.app_name,
            run_id=str(context.run_id),
            correlation_id=context.correlation_id,
            reason=preflight_block_message(e) or str(e),
        )
    else:
        _safe_log(
            "error",
            "App failed",
            app_name=context.app_name,
            run_id=str(context.run_id),
            correlation_id=context.correlation_id,
            error_type=type(e).__name__,
            exc_info=True,
        )


def _as_application_error(e: Exception) -> ApplicationError:
    """The ``ApplicationError`` a failure that is not already a Temporal one fails the run with."""
    if isinstance(e, _NewAppError):
        return ApplicationError(
            str(e),
            e.to_failure_details(),
            type=type(e).__name__,
            non_retryable=not e.effective_retryable,
        )
    return ApplicationError(
        str(e),
        type=type(e).__name__,
        non_retryable=not isinstance(e, RetryableError),
    )


async def _complete_run(
    app_instance: App, context: AppContext, start_time: datetime
) -> None:
    """``on_complete()``, the completion log line, and unbinding the instance."""
    try:
        await app_instance.on_complete()
    except Exception:  # noqa: BLE001 — finally-block cleanup handler; logged via _safe_log with exc_info=True; must not re-raise from finally
        _safe_log(
            "warning",
            "on_complete() hook raised an unexpected exception",
            exc_info=True,
        )

    end_time = _safe_now()
    duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)
    _safe_log(
        "info",
        f"App completed {_BUILD_IDENTITY}",
        app_name=context.app_name,
        run_id=str(context.run_id),
        correlation_id=context.correlation_id,
        duration_ms=duration_ms,
    )

    # The run's app-state slots are not touched here. They live on
    # the workers that ran its tasks, which release them (and close
    # what they hold) once Temporal reports the run closed. This block
    # can neither await a close nor tell a finished run from a cache
    # eviction of a running one, where a task on this worker may
    # still be using what the slot holds.
    app_instance._context = None
    app_instance._run_stored_refs = None
    # BLDX-878: inter-app calls deactivated pending review.
    # app_instance._client = None


def _make_run(
    app_cls: type[App],
    ep: EntryPointMetadata,
    workflow_name: str,
    alias_of_canonical: str | None,
) -> Callable[[_GeneratedWorkflow, object], Awaitable[Output]]:
    """The generated workflow's ``run`` body for one entry point."""
    entry_method_name = ep.method_name
    entrypoint_name = ep.name
    input_type = ep.input_type
    app_name = app_cls._app_name
    app_version = app_cls._app_version
    # Read once here, outside the workflow body: it is an image ENV, constant for
    # the life of the process, and reading it inside _run would be a non-
    # deterministic os.environ touch in the Temporal sandbox.
    app_build_id = app_cls._app_build_id

    # input_data is annotated ``Any`` (not ``input_type``) on the generated
    # method so Temporal's data converter decodes the payload without
    # validating it — validation happens below via _validate_workflow_input so a
    # bad payload fails the workflow execution (fast, non-retryable) instead of
    # failing the workflow *task* (retried by the server indefinitely). See
    # _validate_workflow_input and generate_workflow_class.
    async def _run(self: _GeneratedWorkflow, input_data: object) -> Output:
        if alias_of_canonical:
            _safe_log(
                "warning",
                f"Workflow started on legacy type '{workflow_name}'; the "
                f"canonical type is '{alias_of_canonical}'. This alias is a "
                f"temporary migration surface — move the caller to the "
                f"canonical type.",
                legacy_workflow_type=workflow_name,
                canonical_workflow_type=alias_of_canonical,
            )

        # Fail fast on a malformed / wrong-typed payload, before any setup runs.
        # Enforces the entry point's typed contract at the workflow boundary.
        #
        # Placement is deliberate — this MUST stay above the setup and the outer
        # try/except below, for two reasons:
        #   1. Downstream code (the preflight gate, _log_summary, the entry method)
        #      requires a validated typed model; it must never see the raw dict the
        #      Any-annotated argument decodes to.
        #   2. It preserves pre-PR behavior: previously a bad payload failed during
        #      Temporal's decode, so neither the _run body nor the on_complete()
        #      finally ran. Moving this into the try/except would run on_complete()
        #      for an input that never entered the workflow proper.
        # (Retryability is not the reason: the raised ApplicationError is a
        # FailureError subclass, so the outer handler's `isinstance(e, FailureError)`
        # branch would bare re-raise it, preserving non_retryable=True either way.)
        validated = _validate_workflow_input(input_data, input_type)

        start_time = _safe_now()
        run_id = workflow.info().run_id
        workflow_id = workflow.info().workflow_id
        correlation_id = _correlation_id_for(run_id)

        # The correlation id needs the start input, so __init__ binds a
        # provisional one; set the resolved one here, in the same dict the
        # task wrappers hold.
        app_instance, context_data = _instance_for_run(self, app_cls, start_time)
        context_data["correlation_id"] = correlation_id
        context = AppContext(
            app_name=app_name,
            app_version=app_version,
            build_id=app_build_id,
            run_id=run_id,
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            started_at=start_time,
        )
        app_instance._context = context

        _safe_log(
            "info",
            f"App started {_BUILD_IDENTITY}",
            app_name=app_name,
            run_id=str(run_id),
            correlation_id=context.correlation_id,
        )
        _log_input_summary(validated, context)

        try:
            await _run_preflight_gate(
                validated,
                app_name,
                entrypoint_name,
                getattr(app_cls, "preflight_gate_timeout_seconds", None),
                getattr(app_cls, "preflight_gate_max_attempts", None),
                getattr(app_cls, "preflight_gate_mode", None),
                getattr(app_cls, "preflight_warmup_ceiling_seconds", None),
                getattr(app_cls, "preflight_warmup_probe_timeout_seconds", None),
                getattr(app_cls, "preflight_warmup_mode", None),
            )
            entry_method = getattr(app_instance, entry_method_name)
            result = await entry_method(validated)
            await _merge_skipped_records(app_instance)
            await _merge_fetch_failures(app_instance)
            return cast("Output", result)

        # conformance: ignore[E004] top-level entrypoint handler; logged via _safe_log with exc_info=True and re-raised as typed ApplicationError
        except Exception as e:
            # Non-determinism (a sandbox RestrictedWorkflowAccessError, e.g. a
            # datetime.now() in run()) is a fault in the code, not in the run:
            # let it fail the workflow TASK, which Temporal retries, so a fixed
            # deployment resumes the run. Wrapping it below would fail the run
            # for good and lose it.
            if isinstance(e, workflow.NondeterminismError):
                raise
            _log_run_failure(e, context)
            lifted = _lift_non_retryable_activity_failure(e)
            if lifted is None and isinstance(e, FailureError):
                raise
            raise (lifted or _as_application_error(e)) from e

        finally:
            await _complete_run(app_instance, context, start_time)

    return _run


def _make_wf_init(
    app_cls: type[App], cls_name: str
) -> Callable[[_GeneratedWorkflow], None]:
    """The generated workflow's ``__init__``: build and bind the run's App instance."""

    def _wf_init(self: _GeneratedWorkflow) -> None:
        # Build the per-run App instance and bind it to the run here, not in
        # _run. Temporal applies the @signal / @update jobs of the run's first
        # activation (signal-with-start, update-with-start) after __init__ but
        # before the run coroutine starts, so a handler there must already see
        # @task methods wrapped as activities. Unwrapped, the task body would
        # run inline on the workflow thread: no retries, no timeouts, and
        # re-run on every replay.
        self._app_instance = app_cls()
        # Outside a workflow (unit tests constructing wf_cls directly) there
        # is no run to bind to; _run binds on its fallback path instead.
        #
        # The patch marker keeps runs started before this change replaying
        # their own history: there a first-activation handler ran its task
        # inline and recorded no activity, so binding here would schedule one
        # on replay and fail the run with a nondeterminism error. Live,
        # ``patched`` is True and records the marker; on replay it is True only
        # when the history has it, so such runs keep binding in _run.
        if workflow.in_workflow() and workflow.patched(_EARLY_RUN_BINDING_PATCH):
            self._run_context_data = _bind_app_instance_to_run(
                app_cls, self._app_instance, _safe_now()
            )

    _wf_init.__name__ = "__init__"
    _wf_init.__qualname__ = f"{cls_name}.__init__"
    _wf_init.__module__ = app_cls.__module__
    return _wf_init


def generate_workflow_class(
    app_cls: "type[App]",
    ep: "EntryPointMetadata",
    workflow_name: str | None = None,
) -> type:
    """Generate a Temporal workflow class for one entry point.

    Creates a @workflow.defn-decorated class whose run() sets up App context,
    then calls the entry point method on a fresh App instance.

    Args:
        app_cls: The App subclass.
        ep: The entry point to generate a workflow class for.
        workflow_name: The Temporal workflow type to register this class under.
            An entry point carrying ``legacy_workflow_types`` aliases registers
            under more than one name, so the caller picks; defaults to the
            canonical type.

    Returns:
        A Temporal workflow class decorated with @workflow.defn.
    """
    if workflow_name is None:
        workflow_name = canonical_workflow_type(app_cls._app_name, ep)

    cache_key = (app_cls, ep.name, workflow_name)
    if cache_key in _workflow_class_cache:
        return _workflow_class_cache[cache_key]

    canonical_type = canonical_workflow_type(app_cls._app_name, ep)
    alias_of_canonical = None if workflow_name == canonical_type else canonical_type
    # Boot-time advice, said once by the host. The sandbox rebuilds this class
    # for every run (see _install_workflow_classes) and must not repeat it.
    if alias_of_canonical is None:
        _warn_if_gate_cannot_run(ep.input_type, ep.name, workflow_name)

    cls_name = f"_Workflow_{workflow_type_class_segment(workflow_name)}"

    _run = _make_run(app_cls, ep, workflow_name, alias_of_canonical)
    # Temporal's _is_unbound_method_on_cls checks:
    #   fn.__qualname__.rsplit(".", 1)[0] == cls.__name__
    # so we must set __qualname__ BEFORE applying @workflow.run.
    _run.__name__ = "run"
    _run.__qualname__ = f"{cls_name}.run"
    _run.__module__ = app_cls.__module__
    # input_data is decoded as ``Any`` on purpose: this is the annotation Temporal's
    # pydantic data converter reads to decode the workflow task. Decoding as ``Any``
    # keeps validation out of the converter (where a failure = an indefinitely
    # retried Workflow Task failure) and defers it to _validate_workflow_input inside
    # the run body, which enforces ``input_type`` and fails fast on a bad payload.
    # The return type stays typed so results are still validated/serialized normally.
    _run.__annotations__ = {"input_data": Any, "return": ep.output_type}

    decorated_run = workflow.run(_run)

    # Collect any @signal / @query / @update runtime interactions declared on
    # the App subclass. Each is rewritten into a relay whose Temporal-discovery
    # metadata points at the relay (so the wf_cls is what's registered, not the
    # App class) and whose body delegates to ``self._app_instance.<method>``
    # — sharing state with _run.
    wf_methods: dict[str, object] = {
        "run": decorated_run,
        "__init__": _make_wf_init(app_cls, cls_name),
    }
    wf_methods.update(_collect_interaction_relays(app_cls, cls_name))

    wf_cls = type(cls_name, (), wf_methods)
    wf_cls.__module__ = app_cls.__module__
    wf_cls.__qualname__ = cls_name

    workflow.defn(name=workflow_name)(wf_cls)

    # Temporal's sandbox runner imports the workflow class by name from its
    # __module__.  Since this class is generated dynamically it's never added
    # to the module's namespace automatically, so we do it here.

    _src_module = sys.modules.get(app_cls.__module__)
    if _src_module is not None:
        setattr(_src_module, cls_name, wf_cls)

    # Inside the sandbox, app_cls is the run's own re-imported copy, and so is
    # the module above (sys.modules is the sandbox's while it imports). The
    # cache is host state: an entry would keep every run's sandbox alive.
    if not workflow.unsafe.in_sandbox():
        _workflow_class_cache[cache_key] = wf_cls
    return wf_cls


def _install_workflow_classes(app_cls: "type[App]") -> None:
    """Rebuild an app's generated workflow classes on its sandbox copy.

    Temporal's sandbox runs a workflow by re-importing the module named by the
    workflow class's ``__module__`` (the app's module) and reading the class
    from it by name. The host built those classes after import and attached
    them to the host module, so the fresh copy does not have them. Building
    them again from the re-imported App class, during that import, puts them
    where the sandbox looks, and makes ``run()`` and every helper it calls
    sandboxed code that Temporal checks for non-determinism.

    Called only inside the sandbox, from ``App.__init_subclass__``, through
    ``app.base._sandbox_workflow_installer``.
    """
    for workflow_type, ep in app_cls._app_metadata.workflow_types.items():
        generate_workflow_class(app_cls, ep, workflow_type)


def _wrap_instance_tasks(app_instance: App, context_data: dict[str, Any]) -> None:
    """Wrap @task methods on an instance to execute as Temporal activities.

    Args:
        app_instance: The app instance.
        context_data: Context dict with run_id, workflow_id, and correlation_id.
    """
    for attr_name in dir(app_instance):
        if attr_name.startswith("_"):
            continue

        attr = getattr(type(app_instance), attr_name, None)
        if attr is None:
            continue

        if is_task(attr):
            task_meta = get_task_metadata(attr)
            if task_meta:
                wrapper = _create_task_activity_wrapper(
                    app_instance._app_name,
                    task_meta.name,
                    task_meta.timeout_seconds,
                    task_meta.retry_max_attempts,
                    task_meta.retry_max_interval_seconds,
                    task_meta.output_type,
                    context_data,
                    task_meta.heartbeat_timeout_seconds,
                    task_meta.auto_heartbeat_seconds,
                    task_meta.retry_policy,
                    retry_initial_interval_seconds=task_meta.retry_initial_interval_seconds,
                    pool=task_meta.pool,
                    schedule_to_close_seconds=task_meta.schedule_to_close_seconds,
                    progress_watchdog=task_meta.progress_watchdog,
                    max_no_progress_seconds=task_meta.max_no_progress_seconds,
                    stored_refs=app_instance._run_stored_refs,
                )
                setattr(app_instance, attr_name, wrapper)


class _OptionalDispatch(TypedDict, total=False):
    """The dispatch keywords a task sends only when it sets them."""

    #: The task's declared ceiling on its retry product.
    schedule_to_close_timeout: timedelta
    #: The task queue of the task's worker pool.
    task_queue: str


def _create_task_activity_wrapper(
    app_name: str,
    task_name: str,
    timeout_seconds: int,
    retry_max_attempts: int,
    retry_max_interval_seconds: int,
    output_type: type,
    context_data: dict[str, Any],
    heartbeat_timeout_seconds: int | None = 60,
    auto_heartbeat_seconds: int | None = 10,
    retry_policy: _RP | None = None,
    *,
    retry_initial_interval_seconds: int = 1,
    pool: str | None = None,
    schedule_to_close_seconds: int | None = None,
    progress_watchdog: "ProgressWatchdogMode | None" = None,
    max_no_progress_seconds: float | None = None,
    stored_refs: _RunStoredRefs | None = None,
) -> Callable[[Input], Awaitable[Output]]:
    """Create a wrapper that executes a task as a Temporal activity.

    Args:
        app_name: Name of the app.
        task_name: Name of the task (simple name, no prefix).
        timeout_seconds: Activity timeout for one attempt (``start_to_close``).
        retry_max_attempts: Maximum retry attempts.
        retry_max_interval_seconds: Maximum interval between retries.
        output_type: The typed output class for deserialization.
        context_data: Context dict with run_id, workflow_id, and correlation_id.
        heartbeat_timeout_seconds: Heartbeat timeout. None disables.
        auto_heartbeat_seconds: Auto-heartbeat interval. None disables.
        retry_policy: Full retry policy (overrides max_attempts/interval if set).
        retry_initial_interval_seconds: Delay before the first retry. Later
            delays grow from it by the backoff coefficient, so this is what
            decides how wide a window a small attempt budget spans. Keyword-only
            and defaulting to Temporal's own 1 second, so the positional
            signature this function has always had is unchanged.
        pool: Logical worker-pool name. When set, the activity is routed
            to a dedicated task queue. Queue name resolution order:
            1. ``ATLAN_POOL_<POOL>_QUEUE`` env var (explicit override).
            2. ``{ATLAN_TASK_QUEUE}-{pool}`` derived from the app's base queue
               (default — ensures different apps with the same pool name get
               different Temporal queues automatically).
        schedule_to_close_seconds: Ceiling on the task's total time across every
            attempt. ``None`` (the default) sends no ``schedule_to_close_timeout``
            and leaves the retry product unbounded — ``retry_max_attempts ×
            timeout_seconds``. Resolved against ``timeout_seconds`` by
            :func:`~application_sdk.execution.retry.resolve_activity_time_bounds`.
        progress_watchdog: The task's declared stall-watchdog mode, or ``None``
            to inherit the fleet-wide setting. Forwarded verbatim onto the
            ``TaskContext``; the activity side resolves it, so this path holds
            no opinion about what the default is.
        max_no_progress_seconds: The task's declared no-progress allowance in
            seconds, or ``None`` to inherit the fleet-wide one. Forwarded
            verbatim, for the same reason.
        stored_refs: The run's sink for stored refs. When set, the refs in
            each call's input and result are recorded in it, for
            ``cleanup_storage``.

    Returns:
        Async function that executes the task as an activity.
    """
    # Resolve pool → task queue at construction time. Env vars are fixed for
    # the process lifetime, so capturing the result in the closure is safe.
    # Resolution order: explicit ATLAN_POOL_<POOL>_QUEUE override first, then
    # derive from ATLAN_TASK_QUEUE so two apps sharing a pool name (e.g.
    # "heavy") never collide on the same Temporal queue.
    pool_queue: str | None = resolve_pool_queue(pool) if pool else None

    # Resolved once, outside the wrapper: the pair is fixed for the task's
    # lifetime, so a dispatch does no arithmetic.
    start_to_close_seconds, total_seconds = resolve_activity_time_bounds(
        timeout_seconds, schedule_to_close_seconds
    )
    start_to_close_timeout = timedelta(seconds=start_to_close_seconds)
    schedule_to_close_timeout = (
        timedelta(seconds=total_seconds) if total_seconds is not None else None
    )

    # Build the Temporal RetryPolicy once (not per invocation)
    if retry_policy is not None:
        temporal_retry_policy = _to_temporal_retry_policy(retry_policy)
    else:
        temporal_retry_policy = _to_temporal_retry_policy(
            _RP(
                max_attempts=retry_max_attempts,
                initial_interval=timedelta(seconds=retry_initial_interval_seconds),
                max_interval=timedelta(seconds=retry_max_interval_seconds),
            )
        )

    def _record_stored_refs(data: object) -> None:
        """Record *data*'s stored refs in the run's sink, if it has one.

        *data* is the input this workflow sent or the result it decoded,
        both from history, so recording is deterministic on replay. Never
        fails the task: a ref left out is only one cleanup misses.
        """
        if stored_refs is None:
            return
        try:
            stored_refs.record(_find_file_refs(data))
        except Exception:  # noqa: BLE001 — logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
            _safe_log(
                "warning",
                f"Could not record the stored refs of task {task_name} for "
                "cleanup_storage",
                exc_info=True,
            )

    async def wrapper(input_data: Input) -> Output:
        # Create the task context (metadata for the activity)
        task_context = TaskContext(
            app_name=app_name,
            task_name=task_name,
            run_id=context_data.get("run_id", ""),
            workflow_id=context_data.get("workflow_id", LOCAL_WORKFLOW_ID),
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            auto_heartbeat_seconds=auto_heartbeat_seconds,
            # The workflow is the only side that knows when the *run* started —
            # nothing in `activity.info()` carries it — and the activity needs it
            # for the run-length SLA alert (ADR-0018 → *Bounding total time*).
            # Sourced from history rather than a clock, so a replayed run
            # measures its real age instead of restarting the clock.
            run_started_at_epoch=_run_started_at_epoch(),
            # The task's own declaration, unresolved. Both are `None` for the
            # overwhelming majority of tasks, which is the point: nothing here
            # has to know that the fleet default is `warn` (ADR-0018), and an
            # operator changing it on the worker does not need every workflow to
            # be re-dispatched to take effect.
            progress_watchdog=progress_watchdog,
            max_no_progress_seconds=max_no_progress_seconds,
        )

        # Build heartbeat timeout if enabled
        heartbeat_timeout = (
            timedelta(seconds=heartbeat_timeout_seconds)
            if heartbeat_timeout_seconds is not None
            else None
        )

        # Input refs first, before the activity can fail: a task that fails
        # still received them, and cleanup_storage must still see them.
        _record_stored_refs(input_data)

        # Extract summary from input for Temporal UI display
        summary = input_data.summary() if hasattr(input_data, "summary") else None

        # Execute as activity, routed through the SDK eviction-retry loop so
        # worker pod evictions (SIGTERM mid-activity) re-dispatch as fresh
        # attempts without burning the application-error retry budget.
        # When a pool_queue is set the activity is dispatched to the task
        # queue for that pool; otherwise it runs on the workflow's own queue.
        # Sent only when set. Passing ``schedule_to_close_timeout=None`` would
        # be equivalent, but omitting the key keeps the call byte-identical to
        # before this knob existed for every task that has not opted in.
        optional: _OptionalDispatch = {}
        if schedule_to_close_timeout is not None:
            optional["schedule_to_close_timeout"] = schedule_to_close_timeout
        if pool_queue:
            optional["task_queue"] = pool_queue
        result: Output = await execute_activity_with_eviction_retry(
            f"{app_name}:{task_name}",
            args=[task_context, input_data],
            start_to_close_timeout=start_to_close_timeout,
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=temporal_retry_policy,
            result_type=output_type,
            summary=summary,
            **optional,
        )

        _record_stored_refs(result)
        return result

    return wrapper


# Registered on import: the worker imports this module (through
# ``execution._temporal.workflows``) before any workflow runs in the sandbox.
_app_base._sandbox_workflow_installer = _install_workflow_classes

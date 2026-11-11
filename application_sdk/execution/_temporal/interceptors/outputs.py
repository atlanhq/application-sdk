"""Output interceptor for structured workflow outputs (v3).

This module provides a Temporal interceptor that automatically collects
metrics and artifacts from activities and merges them into the workflow's
return value. This enables the Automation Engine CUE UI to display metrics
like "Total Assets Extracted: 232,008".

v3 adaptation:
    In v3 App.run() returns a Pydantic Output model, not a dict.  The workflow
    interceptor detects this and merges collected outputs into the model via
    model_dump() / model_validate() so the return type stays correct for
    Temporal's pydantic_data_converter.

Architecture:
    - OutputActivityInboundInterceptor: Creates a fresh collector per activity
      and writes what it collected onto the activity's own result
      (``metrics`` / ``artifacts`` on an ``Output`` model, or the reserved
      ``_atlan_outputs`` key on a dict result).
    - _OutputWorkflowOutboundInterceptor: Reads those fields back from every
      completed activity (and local activity) into the workflow's collector.
    - OutputWorkflowInboundInterceptor: At workflow exit, merges the
      workflow's collector into the return value.
    - OutputInterceptor: Top-level interceptor registering both components.

    Outputs travel through Temporal history on the activity result, never
    through process memory. An activity and the workflow that scheduled it
    may run in different worker processes (more than one replica, a restart,
    a dedicated pool queue), and a replay on another worker must rebuild the
    same metrics; a process-local store satisfies neither.

Usage:
    Activities call get_outputs().add_metric(...) during execution.
    The interceptor handles the rest automatically.
"""

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Any, Optional, Type

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    StartActivityInput,
    StartLocalActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.contracts.base import Output
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs import current_outputs
from application_sdk.outputs.collector import OutputCollector

logger = get_logger(__name__)

_METRICS_KEY = "metrics"
_ARTIFACTS_KEY = "artifacts"
# Dict results carry outputs under one reserved key rather than ``metrics`` /
# ``artifacts``: a dict activity may return keys of those names as its own
# data, and the workflow must never mistake that data for collected outputs.
_DICT_CARRIER_KEY = "_atlan_outputs"


def _attach_outputs(result: Any, collector: OutputCollector) -> Any:
    """Write an activity's collected outputs onto its result.

    - ``Output`` model: ``metrics`` / ``artifacts`` are set to exactly this
      activity's outputs, and cleared when it collected none. A task that
      returns an upstream task's output unchanged must not carry that
      upstream task's metrics a second time, or the workflow would count
      them twice.
    - dict: the collected namespaces are added under the reserved
      ``_atlan_outputs`` key; the workflow interceptor strips it from a dict
      workflow result. Left untouched when nothing was collected.
    - anything else cannot carry outputs; they are dropped with a warning.

    Returns:
        The result to hand back to Temporal (a copy when it changed).
    """
    data = collector.to_dict()
    if isinstance(result, Output):
        metrics = data.get(_METRICS_KEY)
        artifacts = data.get(_ARTIFACTS_KEY)
        if result.metrics == metrics and result.artifacts == artifacts:
            return result
        return result.model_copy(
            update={_METRICS_KEY: metrics, _ARTIFACTS_KEY: artifacts}
        )
    if not data:
        return result
    if isinstance(result, dict):
        return {**result, _DICT_CARRIER_KEY: data}
    logger.warning(
        "Activity %s recorded outputs but returned %s, which cannot carry them "
        "back to the workflow; return an Output model to keep them. Dropped "
        "metrics: %s; dropped artifacts: %s",
        activity.info().activity_type,
        type(result).__name__,
        sorted(data.get(_METRICS_KEY, {})),
        sorted(data.get(_ARTIFACTS_KEY, {})),
    )
    return result


def _carried_outputs(result: Any) -> Optional[OutputCollector]:
    """Read the outputs an activity carried on its result, if any."""
    if isinstance(result, Output):
        metrics, artifacts = result.metrics, result.artifacts
    elif isinstance(result, Mapping):
        # Only the reserved key: a dict's own ``metrics`` / ``artifacts`` keys
        # are its data, never collected outputs.
        carried = result.get(_DICT_CARRIER_KEY)
        if not isinstance(carried, Mapping):
            return None
        metrics, artifacts = carried.get(_METRICS_KEY), carried.get(_ARTIFACTS_KEY)
    else:
        return None
    # Only the shapes ``to_dict()`` writes.
    if not isinstance(metrics, Mapping):
        metrics = None
    if not isinstance(artifacts, Mapping):
        artifacts = None
    if not metrics and not artifacts:
        return None
    return OutputCollector.from_dict(metrics, artifacts)


class OutputActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor for collecting activity outputs.

    Creates a fresh OutputCollector for each activity execution and writes
    what it collected onto the activity's result, so the outputs reach the
    workflow through Temporal history whichever process runs the workflow.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute an activity with output collection.

        Args:
            input: The activity execution input.

        Returns:
            The activity's return value, carrying its collected outputs.
        """
        collector = OutputCollector()
        current_outputs.set(collector)

        try:
            result = await super().execute_activity(input)
        finally:
            # Always reset so a failed/cancelled activity doesn't leak its
            # collector into the next task that runs on this thread.
            current_outputs.set(None)

        try:
            return _attach_outputs(result, collector)
        except Exception:
            # Output collection is non-critical — never let it fail the activity.
            logger.warning(
                "Failed to attach collected outputs to the activity result; "
                "outputs for this activity will be lost.",
                exc_info=True,
            )
            return result


class _OutputWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    """Harvests the outputs each completed activity carried on its result.

    The harvest reads the decoded activity result, which comes from workflow
    history, so it is deterministic and a replay on any worker rebuilds the
    same collector.
    """

    def __init__(
        self, next: WorkflowOutboundInterceptor, collector: OutputCollector
    ) -> None:
        super().__init__(next)
        self._collector = collector
        # Handles whose result has not been merged yet. The done callback
        # normally merges and drops each one; ``flush()`` covers a handle whose
        # callback had not run yet when the workflow returned. Keyed by id()
        # for O(1) removal; a dict keeps start order, so merges (and so
        # last-write-wins on string metrics and artifacts) stay deterministic.
        self._pending: dict[int, asyncio.Future[Any]] = {}

    def start_activity(self, input: StartActivityInput) -> Any:
        return self._harvest(self.next.start_activity(input))

    def start_local_activity(self, input: StartLocalActivityInput) -> Any:
        return self._harvest(self.next.start_local_activity(input))

    def _harvest(self, handle: Any) -> Any:
        # Temporal's own handle is a future; an outbound interceptor further
        # down the chain may instead return a coroutine (the Redis lock
        # interceptor does). Keep each kind its own kind, so callers of
        # ``workflow.start_activity`` still get a cancellable handle.
        if asyncio.isfuture(handle):
            self._pending[id(handle)] = handle
            handle.add_done_callback(self._on_done)
            return handle
        if isinstance(handle, Awaitable):
            return self._await_and_merge(handle)
        return handle

    async def _await_and_merge(self, awaitable: Awaitable[Any]) -> Any:
        result = await awaitable
        self._merge(result)
        return result

    def _on_done(self, fut: "asyncio.Future[Any]") -> None:
        if self._pending.pop(id(fut), None) is None:
            return  # already merged by flush()
        if fut.cancelled() or fut.exception() is not None:
            return
        self._merge(fut.result())

    def flush(self) -> None:
        """Merge every completed handle whose done callback has not run yet."""
        for fut in [f for f in self._pending.values() if f.done()]:
            self._on_done(fut)

    def _merge(self, result: Any) -> None:
        try:
            carried = _carried_outputs(result)
            if carried is not None:
                self._collector.merge(carried)
        except Exception:
            # Output enrichment is non-critical — never let it fail the workflow.
            logger.warning(
                "Failed to read outputs carried on an activity result; "
                "they will be missing from the workflow result.",
                exc_info=True,
            )


class OutputWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for merging outputs at workflow completion.

    Owns the workflow's collector: workflow code's ``get_outputs()`` writes to
    it, and the outbound interceptor merges each completed activity's carried
    outputs into it. After the workflow's run() method completes, merges it
    into the return value under "metrics" and "artifacts"
    namespaces.

    v3 adaptation: when the result is a Pydantic BaseModel (e.g. an Output
    subclass), the merge round-trips through model_dump() / model_validate()
    so Temporal's pydantic_data_converter receives the correct typed model back.
    """

    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self._collector = OutputCollector()
        self._outbound: Optional[_OutputWorkflowOutboundInterceptor] = None

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self._outbound = _OutputWorkflowOutboundInterceptor(outbound, self._collector)
        super().init(self._outbound)

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with output merging.

        Args:
            input: The workflow execution input.

        Returns:
            The workflow result, enriched with collected outputs if any.
        """
        current_outputs.set(self._collector)
        try:
            result = await super().execute_workflow(input)
        finally:
            current_outputs.set(None)

        # Only reached when super().execute_workflow() returned without raising.
        try:
            if isinstance(result, dict) and _DICT_CARRIER_KEY in result:
                # A workflow that returns an activity's dict result as its own
                # must not leak the internal carrier into its output.
                result = {k: v for k, v in result.items() if k != _DICT_CARRIER_KEY}
            if self._outbound is not None:
                self._outbound.flush()
            if self._collector.has_data():
                result = _merge_outputs_into_result(result, self._collector)
        except Exception:
            # Output enrichment is non-critical — never let it fail the workflow.
            logger.warning(
                "Failed to enrich workflow result with collected outputs; "
                "workflow result returned unchanged.",
                exc_info=True,
            )

        return result


def _merge_outputs_into_result(result: Any, collector: OutputCollector) -> Any:
    """Merge collected outputs into a workflow result.

    Handles three cases:

    - dict result: merge directly (v2 / legacy path).
    - Pydantic BaseModel result: round-trip via model_dump/model_validate.
      Output base class declares metrics/artifacts fields so they are
      accepted without any runtime class manipulation.
    - Other: return just the collector dict (fallback).
    """
    from pydantic import (  # noqa: PLC0415 — cold path: pydantic only when serializing
        BaseModel,
    )

    output_data = collector.to_dict()

    if isinstance(result, dict):
        return collector.merge_with(result)

    if isinstance(result, BaseModel):
        model_cls = type(result)
        merged = {**result.model_dump(), **output_data}
        try:
            return model_cls.model_validate(merged)
        except Exception:
            logger.warning(
                "Could not merge outputs into %s model; returning outputs as separate dict",
                model_cls.__name__,
                exc_info=True,
            )
            return output_data

    return output_data


class OutputInterceptor(Interceptor):
    """Temporal interceptor for automatic output collection.

    This interceptor provides structured output collection for both
    workflow and activity executions, enabling metrics and artifacts
    to flow from activities to the workflow's return value.
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions for output collection.

        Args:
            next: The next interceptor in the chain.

        Returns:
            The output activity interceptor wrapping the chain.
        """
        return OutputActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class for output merging.

        Args:
            input: The interceptor input.

        Returns:
            The workflow interceptor class.
        """
        return OutputWorkflowInboundInterceptor

"""Execution layer for running Apps on Temporal.

BOOT-TIME: all re-exports are lazy (PEP 562). The eager version imported
the full _temporal backend + temporalio (Rust bridge, grpc, protobuf) onto
every boot path. Lazy exceptions still work in `except` clauses as long as
they are imported (which triggers resolution) before use.
"""

_LAZY_EXPORTS = {
    "AppWorker": ("application_sdk.execution._temporal.worker", "AppWorker"),
    "ApplicationError": ("application_sdk.execution.errors", "ApplicationError"),
    "RetryPolicy": ("application_sdk.execution.retry", "RetryPolicy"),
    "TemporalActivityError": ("temporalio.exceptions", "ActivityError"),
    "TemporalAuthConfig": ("application_sdk.execution._temporal.auth", "TemporalAuthConfig"),
    "TemporalAuthManager": ("application_sdk.execution._temporal.auth", "TemporalAuthManager"),
    "TemporalCancelledError": ("temporalio.exceptions", "CancelledError"),
    "TemporalChildWorkflowError": ("temporalio.exceptions", "ChildWorkflowError"),
    "TemporalClient": ("temporalio.client", "Client"),
    "TemporalExecutorBackend": ("application_sdk.execution._temporal.backend", "TemporalExecutorBackend"),
    "TemporalTerminatedError": ("temporalio.exceptions", "TerminatedError"),
    "TemporalTimeoutError": ("temporalio.exceptions", "TimeoutError"),
    "TemporalWorkflowFailureError": ("temporalio.client", "WorkflowFailureError"),
    "build_output_path": ("application_sdk.execution._temporal.activity_utils", "build_output_path"),
    "create_data_converter": ("application_sdk.execution._temporal.converter", "create_data_converter"),
    "create_data_converter_for_app": ("application_sdk.execution._temporal.converter", "create_data_converter_for_app"),
    "create_temporal_client": ("application_sdk.execution._temporal.backend", "create_temporal_client"),
    "create_worker": ("application_sdk.execution._temporal.worker", "create_worker"),
    "get_object_store_prefix": ("application_sdk.execution._temporal.activity_utils", "get_object_store_prefix"),
    "needs_lock": ("application_sdk.execution.decorators", "needs_lock"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module_path, attr = entry
    value = getattr(importlib.import_module(module_path), attr)
    globals()[name] = value
    return value

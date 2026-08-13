"""Temporal workflow utilities for App execution."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from application_sdk.app.base import generate_workflow_class
    from application_sdk.app.entrypoint import (
        EntryPointContractError,
        workflow_type_class_segment,
    )
    from application_sdk.app.registry import AppRegistry


_RESERVED_SDR_WORKFLOW_TYPES = frozenset(
    {"sdr:test_auth", "sdr:preflight_check", "sdr:fetch_metadata"}
)


def get_all_app_workflows() -> list[type]:
    """Get generated workflow classes for all registered Temporal workflow types.

    One class per key in each App's ``workflow_types`` index — normally one per
    entry point, and two for an entry point carrying a ``workflow_type``
    override (the override plus its canonical alias), so callers on either name
    reach the same entry point.
    """
    workflows: list[type] = []
    claimed_types: dict[str, str] = {}
    claimed_classes: dict[tuple[str, str], tuple[str, str]] = {}
    app_registry = AppRegistry.get_instance()
    for app_name in app_registry.list_apps():
        app_metadata = app_registry.get(app_name)
        for workflow_type, ep in app_metadata.workflow_types.items():
            if workflow_type in _RESERVED_SDR_WORKFLOW_TYPES:
                raise EntryPointContractError(
                    f"App '{app_name}' registers Temporal workflow type "
                    f"'{workflow_type}', which is reserved for the SDK's SDR "
                    "handler workflows. Choose a different workflow_type."
                )
            claimed_by = claimed_types.get(workflow_type)
            if claimed_by is not None and claimed_by != app_name:
                raise EntryPointContractError(
                    f"Apps '{claimed_by}' and '{app_name}' both register Temporal "
                    f"workflow type '{workflow_type}'. Every workflow type on a "
                    "shared worker must be unique."
                )
            claimed_types[workflow_type] = app_name

            class_segment = workflow_type_class_segment(workflow_type)
            class_key = (app_metadata.app_cls.__module__, class_segment)
            class_claim = claimed_classes.get(class_key)
            if class_claim is not None and class_claim != (app_name, workflow_type):
                other_app, other_type = class_claim
                raise EntryPointContractError(
                    f"Apps '{other_app}' and '{app_name}' register Temporal workflow "
                    f"types '{other_type}' and '{workflow_type}', which both generate "
                    f"workflow class '_Workflow_{class_segment}' in module "
                    f"'{class_key[0]}'. One would overwrite the other."
                )
            claimed_classes[class_key] = (app_name, workflow_type)

            wf_cls = generate_workflow_class(app_metadata.app_cls, ep, workflow_type)
            workflows.append(wf_cls)
    return workflows

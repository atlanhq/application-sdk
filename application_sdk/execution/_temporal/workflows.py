"""Temporal workflow utilities for App execution."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from application_sdk.app.base import generate_workflow_class
    from application_sdk.app.registry import AppRegistry


def get_all_app_workflows() -> list[type]:
    """Get generated workflow classes for all registered Temporal workflow types.

    One class per key in each App's ``workflow_types`` index — normally one per
    entry point, and two for an entry point carrying a ``workflow_type``
    override (the override plus its canonical alias), so callers on either name
    reach the same entry point.
    """
    workflows: list[type] = []
    app_registry = AppRegistry.get_instance()
    for app_name in app_registry.list_apps():
        app_metadata = app_registry.get(app_name)
        for workflow_type, ep in app_metadata.workflow_types.items():
            wf_cls = generate_workflow_class(app_metadata.app_cls, ep, workflow_type)
            workflows.append(wf_cls)
    return workflows

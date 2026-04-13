"""Temporal workflow utilities for App execution."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from application_sdk.app.registry import AppRegistry


def get_all_app_workflows() -> list[type]:
    """Get workflow classes for all registered apps."""
    workflows: list[type] = []
    app_registry = AppRegistry.get_instance()
    for app_name in app_registry.list_apps():
        app_metadata = app_registry.get(app_name)
        workflows.append(app_metadata.app_cls)
    return workflows

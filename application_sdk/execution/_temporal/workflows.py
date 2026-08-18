"""Temporal workflow utilities for App execution."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from application_sdk.app.base import generate_workflow_class
    from application_sdk.app.registry import AppRegistry


def get_all_app_workflows() -> list[type]:
    """Get generated workflow classes for all registered app entry points.

    For each registered App, generates one Temporal workflow class per entry
    point. Multi-entry-point apps produce multiple workflow classes. An entry
    point that declares ``aliases`` also contributes one extra workflow class
    per alias (same run body, registered under the bare alias name) so a legacy
    workflow type keeps resolving to it — the alias is a worker registration
    only, not an additional entry point.
    """
    workflows: list[type] = []
    app_registry = AppRegistry.get_instance()
    for app_name in app_registry.list_apps():
        app_metadata = app_registry.get(app_name)
        for ep in app_metadata.entry_points.values():
            workflows.append(generate_workflow_class(app_metadata.app_cls, ep))
            for alias in getattr(ep, "aliases", ()):
                workflows.append(
                    generate_workflow_class(
                        app_metadata.app_cls, ep, workflow_name_override=alias
                    )
                )
    return workflows

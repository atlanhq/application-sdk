"""Temporal workflow utilities for App execution."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from collections.abc import Mapping

    from application_sdk.app.base import generate_workflow_class
    from application_sdk.app.entrypoint import (
        EntryPointContractError,
        workflow_type_class_segment,
    )
    from application_sdk.app.registry import AppMetadata, AppRegistry


_RESERVED_SDR_WORKFLOW_TYPES = frozenset(
    {"sdr:test_auth", "sdr:preflight_check", "sdr:fetch_metadata"}
)


def _require_declaration_matches_registration(
    app_name: str, app_metadata: "AppMetadata"
) -> None:
    """Refuse to start a worker whose alias declaration diverged from registration.

    The declaration is read exactly once, from the class body, at class
    definition. A post-definition assignment
    (``MyApp.legacy_workflow_types = {...}``) or an in-place mutation of the
    declared dict therefore never registers — the app would boot clean and a
    caller on the unregistered alias would get the exact CNCT-199 symptom this
    surface exists to fix: a started run no worker ever claims. This check puts
    every declaration shape through the same door, loudly, at worker startup.
    """
    declared = app_metadata.app_cls.__dict__.get("legacy_workflow_types")
    normalized = (
        dict(declared)
        if isinstance(declared, Mapping)
        else {}
        if declared is None
        else None
    )
    if normalized != dict(app_metadata.legacy_workflow_types):
        raise EntryPointContractError(
            f"App '{app_name}': legacy_workflow_types on the class "
            f"({declared!r}) no longer matches what registration recorded "
            f"({dict(app_metadata.legacy_workflow_types)!r}). The declaration "
            f"is read once at class definition — declare aliases in the class "
            f"body, never by post-definition assignment or mutation."
        )


def get_all_app_workflows() -> list[type]:
    """Get generated workflow classes for all registered Temporal workflow types.

    One class per key in each App's ``workflow_types`` index — one per entry
    point's canonical type, plus one per declared ``legacy_workflow_types``
    alias, so a caller on either name reaches the same entry point.
    """
    workflows: list[type] = []
    claimed_types: dict[str, str] = {}
    claimed_classes: dict[tuple[str, str], tuple[str, str]] = {}
    app_registry = AppRegistry.get_instance()
    for app_name in app_registry.list_apps():
        app_metadata = app_registry.get(app_name)
        _require_declaration_matches_registration(app_name, app_metadata)
        for workflow_type, ep in app_metadata.workflow_types.items():
            if workflow_type in _RESERVED_SDR_WORKFLOW_TYPES:
                raise EntryPointContractError(
                    f"App '{app_name}' registers Temporal workflow type "
                    f"'{workflow_type}', which is reserved for the SDK's SDR "
                    "handler workflows. Choose a different legacy alias."
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

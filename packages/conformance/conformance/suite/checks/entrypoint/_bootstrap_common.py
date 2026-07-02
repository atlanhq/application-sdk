"""Shared helpers for the entrypoint-conformance checks (P017–P018, BLDX-1411)."""

from __future__ import annotations

from conformance.suite.checks._ast_common import (
    collect_import_origins,
    qualify_chained_attr_call,
)

__all__ = [
    "collect_import_origins",
    "is_app_receiver",
    "qualify_chained_attr_call",
]


def is_app_receiver(name: str, origin: str) -> bool:
    """True if the call receiver is plausibly an Atlan App or SDK object.

    Receiver restriction reduces false positives for lifecycle-method checks:
    Temporal's ``Client.start_workflow`` and stdlib ``asyncio.start_server``
    share the same method names but are not app-lifecycle calls.
    """
    return name in {"self", "app"} or origin.startswith("application_sdk")

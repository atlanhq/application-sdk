"""Shared helpers for the entrypoint-conformance checks (P017–P018, BLDX-1411)."""

from __future__ import annotations

from conformance.suite.checks._ast_common import (
    collect_import_origins,
    qualify_chained_attr_call,
)

__all__ = [
    "collect_import_origins",
    "is_app_receiver",
    "is_integration_test_harness",
    "qualify_chained_attr_call",
]


def is_app_receiver(name: str, origin: str) -> bool:
    """True if the call receiver is plausibly an Atlan App or SDK object.

    Receiver restriction reduces false positives for lifecycle-method checks:
    Temporal's ``Client.start_workflow`` and stdlib ``asyncio.start_server``
    share the same method names but are not app-lifecycle calls.
    """
    return name in {"self", "app"} or origin.startswith("application_sdk")


def is_integration_test_harness(file: str) -> bool:
    """True if *file* lives under a ``tests/integration/`` tree.

    Integration-test harnesses construct a worker and Temporal client directly,
    in-process, so the test can get a handle to submit workflows against and
    tear down per test. ``run_dev_combined`` has no non-blocking,
    handle-returning mode that supports this (BLDX-1411), so it cannot replace
    this pattern the way it can for app boot code.

    Scoped to the ``tests/integration/`` path shape specifically — a ``tests``
    segment *immediately followed by* an ``integration`` segment — not to any
    path that merely contains both segments somewhere (``tests/helpers/integration/``
    would not qualify) or to test files in general, so a hand-rolled worker
    anywhere else in the test tree, e.g. a unit test wrongly reaching for
    ``create_worker`` instead of a mock, is still caught.
    """
    parts = file.replace("\\", "/").split("/")
    return any(a == "tests" and b == "integration" for a, b in zip(parts, parts[1:]))

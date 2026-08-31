"""The shared outcome-event name registry.

These strings are the message *body* of a log line, and dashboards, alert rules and
connector-pulse queries match on them exactly. Moving them into one module was the
point of the registry; the risk the move introduces is that one of them gets
"tidied" in transit, which no consumer would notice until a panel silently emptied.

So the values are asserted literally here, and each emitting module is asserted to
still expose the name it always exposed — a shipped consumer importing
``preflight_gate.PREFLIGHT_OUTCOME_EVENT`` must keep working.
"""

from __future__ import annotations

from application_sdk.observability import events


def test_existing_names_moved_unchanged() -> None:
    """The registry is a move, not a rename. Nothing here may ever be reworded."""
    assert events.PREFLIGHT_OUTCOME_EVENT == "Preflight gate outcome"
    assert events.PREFLIGHT_POSTURE_EVENT == "Preflight gate posture"
    assert events.ASSET_VALIDATION_EVENT == "Transformed-asset validation outcome"


def test_the_new_names_are_the_fourth_and_fifth() -> None:
    assert events.ARTIFACT_VALIDATION_EVENT == "Artifact validation outcome"
    assert events.ARTIFACT_VALIDATION_POSTURE_EVENT == "Artifact validation posture"
    assert len(events.OUTCOME_EVENT_NAMES) == 6


def test_the_interactive_preflight_name() -> None:
    """FND-901: the interactive-surface row, distinct from the gate's body so
    gate dashboards are not polluted by setup-time checks."""
    assert events.PREFLIGHT_CHECK_EVENT == "Preflight check outcome"


def test_names_are_distinct() -> None:
    """Two events sharing a body are indistinguishable downstream."""
    names = [
        events.PREFLIGHT_OUTCOME_EVENT,
        events.PREFLIGHT_POSTURE_EVENT,
        events.PREFLIGHT_CHECK_EVENT,
        events.ASSET_VALIDATION_EVENT,
        events.ARTIFACT_VALIDATION_EVENT,
        events.ARTIFACT_VALIDATION_POSTURE_EVENT,
    ]
    assert len(set(names)) == len(names)
    assert all(name.strip() for name in names)


def test_registry_is_the_full_set() -> None:
    assert events.OUTCOME_EVENT_NAMES == {
        events.PREFLIGHT_OUTCOME_EVENT,
        events.PREFLIGHT_POSTURE_EVENT,
        events.PREFLIGHT_CHECK_EVENT,
        events.ASSET_VALIDATION_EVENT,
        events.ARTIFACT_VALIDATION_EVENT,
        events.ARTIFACT_VALIDATION_POSTURE_EVENT,
    }


def test_emitting_modules_still_export_their_names() -> None:
    """v3 has shipped: an existing import site must not break on the move."""
    from application_sdk.app.base import ASSET_VALIDATION_EVENT
    from application_sdk.execution._temporal.preflight_gate import (
        PREFLIGHT_CHECK_EVENT,
        PREFLIGHT_OUTCOME_EVENT,
        PREFLIGHT_POSTURE_EVENT,
    )

    assert PREFLIGHT_OUTCOME_EVENT == events.PREFLIGHT_OUTCOME_EVENT
    assert PREFLIGHT_POSTURE_EVENT == events.PREFLIGHT_POSTURE_EVENT
    assert PREFLIGHT_CHECK_EVENT == events.PREFLIGHT_CHECK_EVENT
    assert ASSET_VALIDATION_EVENT == events.ASSET_VALIDATION_EVENT


def test_the_registry_imports_nothing_from_the_sdk() -> None:
    """A caller that only needs to *name* an event must not pay for the exporter
    stack ``logger_adaptor`` pulls in."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(events.__file__).read_text())
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {"typing", "__future__"}

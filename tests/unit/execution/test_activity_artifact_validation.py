"""Both artifact-validation hooks fire from the real interceptor (FND-691).

:mod:`tests.unit.validation.test_interceptor` exercises the hook directly. This
module asserts the *wiring*: that a real activity built by
``create_activity_from_task`` runs the consumer-side check after materialise and
the producer-side check before persist, that the boundary set resolved at worker
build reaches both, and that a validator blowing up neither fails the activity nor
skips the rest of the hand-off.

The App classes are defined inside each test rather than at module scope because
``tests/unit/execution/conftest.py`` resets both registries around every test — a
module-level App would be registered once at import and gone by the first
``create_activity_from_task``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import orjson
import pytest

from application_sdk.app.base import App
from application_sdk.app.entrypoint import entrypoint
from application_sdk.app.registry import TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.observability.events import ARTIFACT_VALIDATION_EVENT
from application_sdk.validation import interceptor as interceptor_module


class _EchoIn(Input, allow_unbounded_fields=True):
    raw_queries: FileReference | None = None


class _EchoOut(Output, allow_unbounded_fields=True):
    queries: FileReference | None = None


class _ScratchIn(Input, allow_unbounded_fields=True):
    scratch_in: FileReference | None = None


class _ScratchOut(Output, allow_unbounded_fields=True):
    scratch_out: FileReference | None = None


_DECLARATIONS = {
    "raw_queries": {
        "format": "ndjson",
        "fields": [{"name": "QUERY_ID", "type": "string", "description": "id"}],
    },
    "queries": {
        "format": "ndjson",
        "fields": [
            {"name": "QUERY_ID", "type": "string", "description": "id"},
            {"name": "START_TIME", "type": "timestamp", "description": "when"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _boundary_activity(output_path: str) -> Any:
    """An app whose ``@task`` contracts *are* its entry point's contracts."""

    class ArtifactBoundaryApp(App):
        @task(timeout_seconds=60)
        async def echo(self, input: _EchoIn) -> _EchoOut:
            return _EchoOut(queries=FileReference(local_path=output_path))

        async def run(self, input: _EchoIn) -> _EchoOut:
            return await self.echo(input)

    return _activity_for("artifact-boundary-app", "echo")


def _internal_activity(output_path: str) -> Any:
    """An app whose ``@task`` contracts are app-internal; only ``run()``'s are public."""

    class ArtifactInternalApp(App):
        @task(timeout_seconds=60)
        async def crunch(self, input: _ScratchIn) -> _ScratchOut:
            return _ScratchOut(scratch_out=FileReference(local_path=output_path))

        async def run(self, input: _EchoIn) -> _EchoOut:
            return _EchoOut()

    return _activity_for("artifact-internal-app", "crunch")


def _bundle_activity(output_path: str) -> Any:
    """A bundle: two entry points, one legacy inbound alias, one shared @task."""

    class ArtifactBundleApp(App):
        legacy_workflow_types = {"legacy-extract": "extract"}

        @task(timeout_seconds=60)
        async def echo(self, input: _EchoIn) -> _EchoOut:
            return _EchoOut(queries=FileReference(local_path=output_path))

        @entrypoint(name="extract", default=True)
        async def extract(self, input: _EchoIn) -> _EchoOut:
            return await self.echo(input)

        @entrypoint(name="enrich")
        async def enrich(self, input: _EchoIn) -> _EchoOut:
            return _EchoOut()

    return _activity_for("artifact-bundle-app", "echo")


def _activity_for(app_name: str, task_name: str) -> Any:
    tasks = TaskRegistry.get_instance().get_tasks_for_app(app_name)
    return create_activity_from_task(next(t for t in tasks if t.name == task_name))


def _task_context(app_name: str, task_name: str) -> TaskContext:
    return TaskContext(
        app_name=app_name,
        task_name=task_name,
        run_id="run-fnd691",
        heartbeat_timeout_seconds=None,
        auto_heartbeat_seconds=None,
    )


def _events(logger: Any) -> list[dict[str, Any]]:
    return [
        call.kwargs
        for call in logger.info.call_args_list
        if call.args and call.args[0] == ARTIFACT_VALIDATION_EVENT
    ]


def _ndjson(path: Path, records: list[dict[str, Any]]) -> str:
    path.write_bytes(b"\n".join(orjson.dumps(r) for r in records) + b"\n")
    return str(path)


@pytest.fixture
def generated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test generated tree holding the two declarations above."""
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "artifact_schemas.json").write_bytes(
        orjson.dumps({"version": 1, "schemas": _DECLARATIONS})
    )
    monkeypatch.setattr(
        "application_sdk.validation.sources.CONTRACT_GENERATED_DIR", str(generated)
    )
    return generated


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_hooks_fire_on_a_declared_contract(
    tmp_path: Path, generated_dir: Path
) -> None:
    """One declaration at one site, checked on both sides of the task."""
    output = _ndjson(
        tmp_path / "out.json",
        [{"QUERY_ID": "q1", "START_TIME": "2026-08-25T00:00:00Z"}],
    )
    incoming = _ndjson(tmp_path / "in.json", [{"QUERY_ID": "q0"}])
    activity_fn = _boundary_activity(output)

    with patch.object(interceptor_module, "logger") as logger:
        result = await activity_fn(
            _task_context("artifact-boundary-app", "echo"),
            _EchoIn(raw_queries=FileReference(local_path=incoming)),
        )
        events = _events(logger)

    assert cast(_EchoOut, result).queries is not None
    by_side = {e["artifact_side"]: e for e in events}
    assert set(by_side) == {"ingest", "handoff"}
    assert by_side["ingest"]["artifact_field"] == "raw_queries"
    assert by_side["ingest"]["outcome"] == "clean"
    assert by_side["handoff"]["artifact_field"] == "queries"
    assert by_side["handoff"]["outcome"] == "clean"


@pytest.mark.asyncio
async def test_boundary_is_true_for_an_entrypoint_contract(
    tmp_path: Path, generated_dir: Path
) -> None:
    """No declaration on a public interface is a finding, and says which one."""
    (generated_dir / "artifact_schemas.json").unlink()
    activity_fn = _boundary_activity(str(tmp_path / "absent.json"))

    with patch.object(interceptor_module, "logger") as logger:
        await activity_fn(
            _task_context("artifact-boundary-app", "echo"),
            _EchoIn(raw_queries=FileReference(local_path=str(tmp_path / "in.json"))),
        )
        events = _events(logger)

    assert [e["outcome"] for e in events] == ["not_declared", "not_declared"]
    assert all(e["boundary"] is True for e in events)


@pytest.mark.asyncio
async def test_boundary_is_false_for_an_internal_task_contract(
    tmp_path: Path, generated_dir: Path
) -> None:
    """The same missing declaration is informational inside the app."""
    (generated_dir / "artifact_schemas.json").unlink()
    activity_fn = _internal_activity(str(tmp_path / "scratch-out.json"))

    with patch.object(interceptor_module, "logger") as logger:
        await activity_fn(
            _task_context("artifact-internal-app", "crunch"),
            _ScratchIn(scratch_in=FileReference(local_path=str(tmp_path / "s.json"))),
        )
        events = _events(logger)

    assert [e["outcome"] for e in events] == ["not_declared", "not_declared"]
    assert all(e["boundary"] is False for e in events)


@pytest.mark.asyncio
async def test_a_raising_validator_never_reaches_the_task(
    tmp_path: Path, generated_dir: Path
) -> None:
    """The activity still returns its result; the failure lands as ``absent``."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("validator exploded")

    activity_fn = _boundary_activity(
        _ndjson(tmp_path / "out.json", [{"QUERY_ID": "q"}])
    )

    with patch("application_sdk.validation.wrapper.validate_artifact", _boom):
        with patch.object(interceptor_module, "logger") as logger:
            result = await activity_fn(
                _task_context("artifact-boundary-app", "echo"),
                _EchoIn(
                    raw_queries=FileReference(
                        local_path=_ndjson(tmp_path / "in.json", [{"QUERY_ID": "q0"}])
                    )
                ),
            )
            events = _events(logger)

    assert cast(_EchoOut, result).queries is not None
    assert [e["outcome"] for e in events] == ["absent", "absent"]


# ---------------------------------------------------------------------------
# A bundle reads its own entry point's declarations
# ---------------------------------------------------------------------------


@pytest.fixture
def bundle_generated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bundle tree whose two declaration files disagree on purpose.

    ``app/generated/extract/artifact_schemas.json`` declares what the artifact
    actually carries; the flat ``app/generated/artifact_schemas.json`` declares a
    field it does not. The nested file is tried first and the first file that
    exists answers, so a ``clean`` outcome can only mean the per-entry-point file
    was the one read — and a ``flagged`` one would mean the entry point never
    reached the resolver and the run fell through to the flat file.
    """
    generated = tmp_path / "generated"
    (generated / "extract").mkdir(parents=True)
    (generated / "extract" / "artifact_schemas.json").write_bytes(
        orjson.dumps(
            {
                "version": 1,
                "schemas": {
                    "queries": {
                        "format": "ndjson",
                        "fields": [
                            {"name": "QUERY_ID", "type": "string", "description": "id"}
                        ],
                    }
                },
            }
        )
    )
    (generated / "artifact_schemas.json").write_bytes(
        orjson.dumps(
            {
                "version": 1,
                "schemas": {
                    "queries": {
                        "format": "ndjson",
                        "fields": [
                            {
                                "name": "NOT_IN_THE_ARTIFACT",
                                "type": "string",
                                "description": "only the flat file asks for this",
                            }
                        ],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(
        "application_sdk.validation.sources.CONTRACT_GENERATED_DIR", str(generated)
    )
    return generated


@pytest.mark.parametrize(
    "workflow_type",
    [
        pytest.param("artifact-bundle-app:extract", id="canonical-type"),
        pytest.param("legacy-extract", id="legacy-alias"),
    ],
)
@pytest.mark.asyncio
async def test_the_runs_entry_point_selects_the_declaration_file(
    tmp_path: Path, bundle_generated_dir: Path, workflow_type: str
) -> None:
    """The workflow type the run arrives on picks which declarations apply.

    Stubbed at the Temporal boundary rather than at ``_current_workflow_type``, so
    the key this asserts on is the one Temporal actually hands the activity — the
    index is built from ``AppMetadata.workflow_types``, and a legacy inbound alias
    has to resolve to the same entry point as the canonical type.
    """
    output = _ndjson(tmp_path / "out.json", [{"QUERY_ID": "q1"}])
    activity_fn = _bundle_activity(output)

    with patch.object(
        activities_module.activity,
        "info",
        return_value=MagicMock(workflow_type=workflow_type),
    ):
        with patch.object(interceptor_module, "logger") as logger:
            await activity_fn(_task_context("artifact-bundle-app", "echo"), _EchoIn())
            events = _events(logger)

    handoff = next(e for e in events if e["artifact_side"] == "handoff")
    assert handoff["entrypoint"] == "extract"
    assert handoff["outcome"] == "clean", (
        "flagged means the flat file answered — the entry point never reached "
        "ContractSource"
    )


@pytest.mark.asyncio
async def test_an_unregistered_workflow_type_falls_back_to_the_flat_file(
    tmp_path: Path, bundle_generated_dir: Path
) -> None:
    """The conservative direction: read the flat file, never guess an entry point."""
    output = _ndjson(tmp_path / "out.json", [{"QUERY_ID": "q1"}])
    activity_fn = _bundle_activity(output)

    with patch.object(
        activities_module.activity,
        "info",
        return_value=MagicMock(workflow_type="some-type-nobody-registered"),
    ):
        with patch.object(interceptor_module, "logger") as logger:
            await activity_fn(_task_context("artifact-bundle-app", "echo"), _EchoIn())
            events = _events(logger)

    handoff = next(e for e in events if e["artifact_side"] == "handoff")
    assert handoff["entrypoint"] == ""
    # The flat file asks for a field the artifact does not carry.
    assert handoff["outcome"] == "flagged"

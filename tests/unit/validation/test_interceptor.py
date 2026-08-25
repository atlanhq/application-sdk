"""Artifact validation wired to the FileReference interceptor (FND-691, ADR-0020).

The property under test throughout is **no silent no-op**. The hook this replaces
returned early and emitted nothing whenever its path gate did not match, so an app
could look adopted while validating zero records (FND-401) — which is why almost
every test here asserts on what was *emitted*, negatives included, rather than on
whether a scan happened to run.

Events are captured by patching the module's own logger. That is enough here
because :mod:`tests.unit.validation.test_artifact_event_fields` already asserts the
attribute map against the real ``_build_extra_dict`` filter — a mock records
whatever it is handed, so only that test can prove the fields reach OTLP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import orjson
import pytest

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.observability.events import ARTIFACT_VALIDATION_EVENT
from application_sdk.storage.file_ref_sync import _find_file_refs, iter_named_file_refs
from application_sdk.validation import interceptor as interceptor_module
from application_sdk.validation.interceptor import (
    ARTIFACT_SIDE_HANDOFF,
    ARTIFACT_SIDE_INGEST,
    boundary_contract_types,
    entrypoint_index,
    validate_artifacts,
)

# ---------------------------------------------------------------------------
# Contracts under test
# ---------------------------------------------------------------------------


class _BoundaryIn(Input, allow_unbounded_fields=True):
    """Stands in for an entry point's public input contract."""

    raw_queries: FileReference | None = None


class _BoundaryOut(Output, allow_unbounded_fields=True):
    """Stands in for an entry point's public output contract."""

    queries: FileReference | None = None


class _InternalOut(Output, allow_unbounded_fields=True):
    """An internal ``@task`` contract — never an entry point's boundary."""

    scratch: FileReference | None = None


class _Nested(Output, allow_unbounded_fields=True):
    inner: FileReference | None = None


class _ParentOut(Output, allow_unbounded_fields=True):
    child: _Nested | None = None
    parts: list[FileReference] = []


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_declarations(generated: Path, schemas: dict[str, Any]) -> None:
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "artifact_schemas.json").write_bytes(
        orjson.dumps({"version": 1, "schemas": schemas})
    )


_NDJSON_QUERIES = {
    "queries": {
        "format": "ndjson",
        "fields": [
            {"name": "QUERY_ID", "type": "string", "description": "the query id"},
            {"name": "START_TIME", "type": "timestamp", "description": "when it ran"},
        ],
    }
}


@pytest.fixture
def generated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the contract source at a per-test generated tree.

    ``sources`` binds ``CONTRACT_GENERATED_DIR`` at import, and the loader caches
    per path — a fresh ``tmp_path`` per test keeps both honest.
    """
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setattr(
        "application_sdk.validation.sources.CONTRACT_GENERATED_DIR", str(generated)
    )
    return generated


def _ndjson(tmp_path: Path, name: str, records: list[dict[str, Any]]) -> str:
    path = tmp_path / name
    path.write_bytes(b"\n".join(orjson.dumps(r) for r in records) + b"\n")
    return str(path)


def _events(logger: Any) -> list[dict[str, Any]]:
    """Every artifact-validation outcome row the hook emitted, in order."""
    return [
        call.kwargs
        for call in logger.info.call_args_list
        if call.args and call.args[0] == ARTIFACT_VALIDATION_EVENT
    ]


async def _run(data: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Run the hook with a captured logger and return the emitted rows."""
    kwargs.setdefault("side", ARTIFACT_SIDE_HANDOFF)
    with patch.object(interceptor_module, "logger") as logger:
        await validate_artifacts(data, **kwargs)
        return _events(logger)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class TestNamedWalk:
    """``iter_named_file_refs`` is the one walk; ``_find_file_refs`` rides it."""

    def test_names_the_contract_field_the_ref_was_reached_through(self) -> None:
        out = _BoundaryOut(queries=FileReference(local_path="/tmp/q.json"))
        named = list(iter_named_file_refs(out))
        assert [(n.field, n.owner) for n in named] == [("queries", _BoundaryOut)]

    def test_container_elements_share_the_container_field(self) -> None:
        parent = _ParentOut(
            parts=[
                FileReference(local_path="/tmp/a.json"),
                FileReference(local_path="/tmp/b.json"),
            ]
        )
        named = list(iter_named_file_refs(parent))
        assert [(n.field, n.owner) for n in named] == [
            ("parts", _ParentOut),
            ("parts", _ParentOut),
        ]

    def test_a_nested_model_owns_its_own_field(self) -> None:
        """So a ref on an inner model is never counted as a boundary hand-off."""
        parent = _ParentOut(child=_Nested(inner=FileReference(local_path="/tmp/i")))
        named = list(iter_named_file_refs(parent))
        assert [(n.field, n.owner) for n in named] == [("inner", _Nested)]

    def test_a_bare_ref_has_no_field_and_no_owner(self) -> None:
        named = list(iter_named_file_refs(FileReference(local_path="/tmp/x")))
        assert (named[0].field, named[0].owner) == ("", None)

    def test_find_file_refs_still_agrees_with_the_named_walk(self) -> None:
        parent = _ParentOut(
            child=_Nested(inner=FileReference(local_path="/tmp/i")),
            parts=[FileReference(local_path="/tmp/a"), FileReference()],
        )
        assert _find_file_refs(parent) == [n.ref for n in iter_named_file_refs(parent)]


# ---------------------------------------------------------------------------
# Worker-build resolution
# ---------------------------------------------------------------------------


class TestWorkerBuildResolution:
    def test_unregistered_app_resolves_to_empty_and_never_raises(self) -> None:
        assert boundary_contract_types("no-such-app-fnd691") == frozenset()
        assert entrypoint_index("no-such-app-fnd691") == {}

    def test_boundary_set_is_the_entry_points_contracts(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.task import task

        class BoundaryProbeApp(App):
            @task(timeout_seconds=60)
            async def crunch(self, input: Input) -> _InternalOut:  # noqa: D102
                return _InternalOut()

            async def run(self, input: _BoundaryIn) -> _BoundaryOut:  # noqa: D102
                return _BoundaryOut()

        assert boundary_contract_types("boundary-probe-app") == frozenset(
            {_BoundaryIn, _BoundaryOut}
        )
        # The internal @task contract is excluded by construction, not by a filter.
        assert _InternalOut not in boundary_contract_types("boundary-probe-app")
        # Every registered workflow type resolves to its entry point, so a bundle
        # reads its own declaration file rather than missing it silently.
        assert entrypoint_index("boundary-probe-app") == {"boundary-probe-app": "run"}


# ---------------------------------------------------------------------------
# Every artifact emits exactly one outcome
# ---------------------------------------------------------------------------


class TestEveryArtifactEmits:
    @pytest.mark.asyncio
    async def test_a_declared_clean_artifact_reports_clean(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        _write_declarations(generated_dir, _NDJSON_QUERIES)
        local = _ndjson(
            tmp_path,
            "queries.json",
            [{"QUERY_ID": "q1", "START_TIME": "2026-08-25T10:00:00Z"}],
        )
        events = await _run(_BoundaryOut(queries=FileReference(local_path=local)))
        assert len(events) == 1
        assert events[0]["outcome"] == "clean"
        assert events[0]["artifact_format"] == "ndjson"
        assert events[0]["artifact_schema_source"] == "contract"
        assert events[0]["artifact_field"] == "queries"
        assert events[0]["artifact_total"] == 1
        assert events[0]["artifact_side"] == ARTIFACT_SIDE_HANDOFF

    @pytest.mark.asyncio
    async def test_a_declared_broken_artifact_reports_flagged(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """The 73-day RCA in miniature: a timestamp that became a string."""
        _write_declarations(generated_dir, _NDJSON_QUERIES)
        local = _ndjson(
            tmp_path,
            "queries.json",
            [{"QUERY_ID": "q1", "START_TIME": "last Tuesday"}],
        )
        with patch.object(interceptor_module, "logger") as logger:
            await validate_artifacts(
                _BoundaryOut(queries=FileReference(local_path=local)),
                side=ARTIFACT_SIDE_HANDOFF,
            )
            events = _events(logger)
            # The human-readable report rides a WARNING only when flagged.
            assert logger.warning.called
        assert events[0]["outcome"] == "flagged"
        assert events[0]["artifact_failed"] == 1
        matrix = orjson.loads(events[0]["artifact_validation_matrix"])
        assert matrix[0]["field"] == "START_TIME"
        assert matrix[0]["expected"] == "timestamp"

    @pytest.mark.asyncio
    async def test_an_undeclared_ref_reports_not_declared(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        local = _ndjson(tmp_path, "scratch.json", [{"anything": 1}])
        events = await _run(_InternalOut(scratch=FileReference(local_path=local)))
        assert [e["outcome"] for e in events] == ["not_declared"]

    @pytest.mark.asyncio
    async def test_an_unsupported_cell_says_so_rather_than_going_quiet(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """A format this SDK has no validator for names itself and is a row.

        The loader deliberately does not police the format vocabulary, so a newer
        contract toolkit can declare a format an older SDK cannot check. The honest
        answer is ``unsupported`` naming the format — not ``absent``, which would
        claim the declaration itself was unreadable, and not silence.
        """
        _write_declarations(
            generated_dir,
            {
                "queries": {
                    "format": "avro",
                    "fields": [{"name": "QUERY_ID", "type": "string"}],
                }
            },
        )
        local = _ndjson(tmp_path, "queries.json", [{"QUERY_ID": "q1"}])
        events = await _run(_BoundaryOut(queries=FileReference(local_path=local)))
        assert events[0]["outcome"] == "unsupported"
        assert events[0]["artifact_format"] == "avro"

    @pytest.mark.asyncio
    async def test_a_missing_artifact_reports_absent(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        _write_declarations(generated_dir, _NDJSON_QUERIES)
        events = await _run(
            _BoundaryOut(
                queries=FileReference(local_path=str(tmp_path / "never-written.json"))
            )
        )
        assert events[0]["outcome"] == "absent"

    @pytest.mark.asyncio
    async def test_a_ref_with_no_local_artifact_still_emits(
        self, generated_dir: Path
    ) -> None:
        """A lazy or unmaterialised ref: declared, so ``absent``, never silence."""
        _write_declarations(generated_dir, _NDJSON_QUERIES)
        events = await _run(
            _BoundaryOut(
                queries=FileReference(storage_path="artifacts/q", is_durable=True)
            )
        )
        assert events[0]["outcome"] == "absent"
        assert events[0]["artifact_schema_source"] == "contract"

    @pytest.mark.asyncio
    async def test_no_local_artifact_and_no_declaration_is_not_declared(
        self, generated_dir: Path
    ) -> None:
        """ "You declared nothing" must not be mislabelled "we could not read it"."""
        events = await _run(_BoundaryOut(queries=FileReference()))
        assert events[0]["outcome"] == "not_declared"

    @pytest.mark.asyncio
    async def test_nothing_is_emitted_for_a_payload_holding_no_refs(self) -> None:
        assert await _run(_BoundaryOut()) == []


# ---------------------------------------------------------------------------
# boundary
# ---------------------------------------------------------------------------


class TestBoundaryAttribution:
    @pytest.mark.asyncio
    async def test_entrypoint_contract_reports_boundary_true(
        self, generated_dir: Path
    ) -> None:
        events = await _run(
            _BoundaryOut(queries=FileReference()),
            boundary_contracts=frozenset({_BoundaryIn, _BoundaryOut}),
        )
        assert events[0]["outcome"] == "not_declared"
        assert events[0]["boundary"] is True

    @pytest.mark.asyncio
    async def test_internal_task_contract_reports_boundary_false(
        self, generated_dir: Path
    ) -> None:
        events = await _run(
            _InternalOut(scratch=FileReference()),
            boundary_contracts=frozenset({_BoundaryIn, _BoundaryOut}),
        )
        assert events[0]["outcome"] == "not_declared"
        assert events[0]["boundary"] is False

    @pytest.mark.asyncio
    async def test_a_ref_on_a_nested_model_is_not_a_boundary_handoff(
        self, generated_dir: Path
    ) -> None:
        events = await _run(
            _ParentOut(child=_Nested(inner=FileReference())),
            boundary_contracts=frozenset({_ParentOut}),
        )
        assert events[0]["boundary"] is False


# ---------------------------------------------------------------------------
# The scaffold may never break the hand-off
# ---------------------------------------------------------------------------


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("validator exploded")


class TestNeverBreaksTheHandoff:
    @pytest.mark.asyncio
    async def test_a_raising_validator_becomes_an_absent_row(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        _write_declarations(generated_dir, _NDJSON_QUERIES)
        local = _ndjson(tmp_path, "queries.json", [{"QUERY_ID": "q1"}])
        with patch("application_sdk.validation.wrapper.validate_artifact", _boom):
            events = await _run(
                _BoundaryOut(queries=FileReference(local_path=local)),
                boundary_contracts=frozenset({_BoundaryOut}),
            )
        assert events[0]["outcome"] == "absent"
        # The boundary fact survives the failure — it is the wiring's, not the
        # validator's, so a broken validator cannot erase a finding's audience.
        assert events[0]["boundary"] is True

    @pytest.mark.asyncio
    async def test_a_broken_walk_is_swallowed(self, generated_dir: Path) -> None:
        with patch.object(interceptor_module, "_walk", _boom):
            assert await _run(_BoundaryOut(queries=FileReference())) == []

    @pytest.mark.asyncio
    async def test_a_broken_emit_is_swallowed(self, generated_dir: Path) -> None:
        with patch.object(
            interceptor_module, "artifact_validation_event_fields", _boom
        ):
            with patch.object(interceptor_module, "logger") as logger:
                # Returning at all is half the assertion: a raise here would have
                # propagated straight into the task the hook wraps.
                await validate_artifacts(
                    _BoundaryOut(queries=FileReference()), side=ARTIFACT_SIDE_INGEST
                )
                assert _events(logger) == []
                assert logger.warning.called


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


class TestBookkeeping:
    @pytest.mark.asyncio
    async def test_identical_refs_are_scanned_once(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        ref = FileReference(local_path=_ndjson(tmp_path, "a.json", [{"x": 1}]))
        events = await _run(_ParentOut(parts=[ref, ref]))
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_distinct_artifacts_under_one_field_each_emit(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        events = await _run(
            _ParentOut(
                parts=[
                    FileReference(local_path=_ndjson(tmp_path, "a.json", [{"x": 1}])),
                    FileReference(local_path=_ndjson(tmp_path, "b.json", [{"x": 2}])),
                ]
            )
        )
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_distinct_durable_refs_under_one_field_each_emit(
        self, generated_dir: Path
    ) -> None:
        """Nothing is materialised yet, so ``local_path`` cannot tell them apart.

        Deduplicating on the local path alone would collapse a whole
        ``list[FileReference]`` of durable artifacts into one row — understating
        the denominator and hiding every hand-off but the first.
        """
        events = await _run(
            _ParentOut(
                parts=[
                    FileReference(storage_path="artifacts/a", is_durable=True),
                    FileReference(storage_path="artifacts/b", is_durable=True),
                ]
            )
        )
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_the_same_durable_artifact_twice_is_scanned_once(
        self, generated_dir: Path
    ) -> None:
        """Two references, one storage path: a genuine repeat, so one row."""
        events = await _run(
            _ParentOut(
                parts=[
                    FileReference(storage_path="artifacts/a", is_durable=True),
                    FileReference(storage_path="artifacts/a", is_durable=True),
                ]
            )
        )
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_refs_naming_no_artifact_at_all_each_emit(
        self, generated_dir: Path
    ) -> None:
        """Neither path is set, so nothing proves these are the same hand-off.

        Dedup may only ever drop a row it can prove is a repeat; two references
        that name nothing fall back to object identity rather than collapsing.
        """
        events = await _run(_ParentOut(parts=[FileReference(), FileReference()]))
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_the_kill_switch_stops_the_hook_entirely(
        self, generated_dir: Path
    ) -> None:
        with patch("application_sdk.constants.VALIDATE_ARTIFACTS", False):
            assert await _run(_BoundaryOut(queries=FileReference())) == []

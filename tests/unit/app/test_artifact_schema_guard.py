"""FND-687: a boundary ``FileReference`` must declare an artifact schema.

Every ``EntryPointMetadata``'s ``input_type``/``output_type`` is a public
interface — another app or the DAG reads it — so every ``FileReference`` on one
needs a declared shape.  An internal ``@task`` contract does not: the app owns
that risk.  In 3.x the gap is a ``DeprecationWarning`` naming the field, the
entry point and the 4.0 removal version; in 4.0 it becomes an error.

Every test defines its App class *inside* the test body: the guard runs from
``App.__init_subclass__``, so class definition is the act under test.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path, PureWindowsPath

import pytest

from application_sdk.app._artifact_schema_guard import (
    ARTIFACT_SCHEMA_REMOVAL_VERSION,
    _Declarations,
    _declared_artifact_schema_keys,
    _mentions_file_reference,
)
from application_sdk.app.base import App
from application_sdk.app.entrypoint import entrypoint
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class DeclaredInput(Input, allow_unbounded_fields=True):
    """Boundary input whose ``raw_queries`` field is declared in the fixture."""

    raw_queries: FileReference | None = None


class UndeclaredOutput(Output, allow_unbounded_fields=True):
    """Boundary output carrying an artifact nothing declares."""

    transformed_entities: FileReference | None = None
    row_count: int = 0


class PlainInput(Input, allow_unbounded_fields=True):
    """Boundary input with no artifact at all."""

    row_count: int = 0


class PlainOutput(Output, allow_unbounded_fields=True):
    """Boundary output with no artifact at all."""

    row_count: int = 0


class TaskInput(Input, allow_unbounded_fields=True):
    """Internal ``@task`` input — exempt from the rule."""

    scratch_file: FileReference | None = None


class TaskOutput(Output, allow_unbounded_fields=True):
    """Internal ``@task`` output — exempt from the rule."""

    scratch_file: FileReference | None = None


def _write_declarations(directory: Path, *field_names: str) -> None:
    """Write a minimal, well-formed ``artifact_schemas.json`` under *directory*.

    Shaped like the toolkit's real output (``version`` + ``schemas`` keyed by
    contract field name) so the guard is exercised against the file it will
    actually meet, not a convenient stand-in.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "artifact_schemas.json").write_text(
        json.dumps(
            {
                "version": 1,
                "schemas": {
                    name: {
                        "format": "ndjson",
                        "fields": [
                            {
                                "name": "typeName",
                                "type": "string",
                                "required": True,
                                "description": "Atlan type this record instantiates.",
                            }
                        ],
                    }
                    for name in field_names
                },
            }
        ),
        encoding="utf-8",
    )


def _write_manifest(directory: Path) -> None:
    """Write the ``manifest.json`` that gives the generated tree its shape.

    The layout is read off this file, exactly as conformance K016 reads it: a
    ``manifest.json`` in per-entry-point subdirectories means a bundle, one at
    the root means a single generated contract. Every fixture writes it, because
    an app whose tree carries no manifest is not a shape either reader can
    classify.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps({"dag": {}}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_registry_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Register into a clean registry, rooted at a scratch repo layout.

    ``CONTRACT_GENERATED_DIR`` is a repo-relative path, so the guard reads
    whatever ``app/generated/`` the process is standing in.
    """
    AppRegistry.reset()
    TaskRegistry.reset()
    monkeypatch.chdir(tmp_path)
    yield
    AppRegistry.reset()
    TaskRegistry.reset()


def _boundary_warnings(recorded: list[warnings.WarningMessage]) -> list[str]:
    """Return the guard's deprecation messages, dropping unrelated warnings."""
    return [
        str(w.message)
        for w in recorded
        if issubclass(w.category, DeprecationWarning)
        and "artifact schema" in str(w.message)
    ]


class TestBoundaryDeclarationRequired:
    def test_undeclared_boundary_field_warns_naming_field_ep_and_version(
        self, tmp_path: Path
    ) -> None:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class UndeclaredApp(App):
                name = "undeclared-app"

                async def run(self, input: DeclaredInput) -> UndeclaredOutput:
                    return UndeclaredOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 2, messages
        joined = "\n".join(messages)
        # Both boundary fields are reported, each naming its own field...
        assert "'raw_queries'" in joined
        assert "'transformed_entities'" in joined
        # ...the entry point it sits on, and the removal version.
        assert "entry point 'run'" in messages[0]
        assert f"v{ARTIFACT_SCHEMA_REMOVAL_VERSION}" in messages[0]
        # The non-artifact field is not reported.
        assert "row_count" not in joined

    def test_declared_boundary_field_is_silent(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path / "app" / "generated")
        _write_declarations(tmp_path / "app" / "generated", "raw_queries")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class DeclaredApp(App):
                name = "declared-app"

                async def run(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

        assert _boundary_warnings(recorded) == []

    def test_contract_without_artifacts_is_silent(self, tmp_path: Path) -> None:
        class NoArtifactInput(Input, allow_unbounded_fields=True):
            connection_qualified_name: str = ""

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class PlainApp(App):
                name = "plain-app"

                async def run(self, input: NoArtifactInput) -> PlainOutput:
                    return PlainOutput()

        assert _boundary_warnings(recorded) == []


class TestInternalTasksAreExempt:
    def test_task_contract_file_reference_does_not_warn(self, tmp_path: Path) -> None:
        """An undeclared ``FileReference`` on a ``@task`` is the app's own call."""

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class TaskExemptApp(App):
                name = "task-exempt-app"

                @task
                async def stage(self, input: TaskInput) -> TaskOutput:
                    return TaskOutput()

                async def run(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        # Only the entry point's own input is reported; neither the task's
        # input nor its output contributes a finding.
        assert len(messages) == 1, messages
        assert "'raw_queries'" in messages[0]
        assert "scratch_file" not in messages[0]


class TestGeneratedLayouts:
    def test_multi_entrypoint_reads_the_nested_declaration(
        self, tmp_path: Path
    ) -> None:
        """A bundle nests each entry point's file under its wire name."""
        _write_manifest(tmp_path / "app" / "generated" / "extract-metadata")
        _write_declarations(
            tmp_path / "app" / "generated" / "extract-metadata", "raw_queries"
        )

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class BundleApp(App):
                name = "bundle-app"

                @entrypoint
                async def extract_metadata(
                    self, input: DeclaredInput
                ) -> UndeclaredOutput:
                    return UndeclaredOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "'transformed_entities'" in messages[0]
        assert "entry point 'extract-metadata'" in messages[0]

    def test_nested_declaration_does_not_answer_for_a_sibling(
        self, tmp_path: Path
    ) -> None:
        """One entry point's declarations never satisfy another's boundary."""
        _write_manifest(tmp_path / "app" / "generated" / "extract-metadata")
        _write_manifest(tmp_path / "app" / "generated" / "mine-queries")
        _write_declarations(
            tmp_path / "app" / "generated" / "extract-metadata", "raw_queries"
        )

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class SiblingApp(App):
                name = "sibling-app"

                @entrypoint
                async def extract_metadata(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

                @entrypoint
                async def mine_queries(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "entry point 'mine-queries'" in messages[0]


class TestTheWarningNamesAnActionablePath:
    """The cited path must be one the toolkit will actually write.

    A bundle emits one file per entry point under its wire name and its root
    may not legally declare ``artifactSchemas`` at all, so citing the flat path
    to a bundle author sends them somewhere the toolkit never writes.
    """

    def test_a_single_entrypoint_app_is_pointed_at_the_flat_file(
        self, tmp_path: Path
    ) -> None:
        _write_manifest(tmp_path / "app" / "generated")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class FlatApp(App):
                name = "flat-app"

                async def run(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "app/generated/artifact_schemas.json" in messages[0]
        assert "app/generated/run/" not in messages[0]

    def test_a_bundle_entrypoint_is_pointed_at_its_own_nested_file(
        self, tmp_path: Path
    ) -> None:
        _write_manifest(tmp_path / "app" / "generated" / "extract-metadata")
        _write_manifest(tmp_path / "app" / "generated" / "mine-queries")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class NestedApp(App):
                name = "nested-app"

                @entrypoint
                async def extract_metadata(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

                @entrypoint
                async def mine_queries(self, input: PlainInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert (
            "app/generated/extract-metadata/artifact_schemas.json" in messages[0]
        ), messages[0]

    def test_a_card_split_app_is_pointed_at_its_flat_file(self, tmp_path: Path) -> None:
        """Several ``@entrypoint``s, one flat generated tree — not a bundle.

        A route/card-split app (BLDX-1342) is where the Python entry-point count
        and the generated layout disagree: one marketplace card, so one flat
        ``artifact_schemas.json``, but several entry points the DAG invokes by
        ``workflow_type``. Counting entry points would call it a bundle and cite
        ``app/generated/<wire-name>/artifact_schemas.json`` — a file the toolkit
        never writes for this app, so following the warning could not clear it.
        """
        _write_manifest(tmp_path / "app" / "generated")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class CardSplitApp(App):
                name = "card-split-app"

                @entrypoint
                async def extract_metadata(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

                @entrypoint
                async def mine_queries(self, input: PlainInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "app/generated/artifact_schemas.json" in messages[0]
        assert "app/generated/extract-metadata/" not in messages[0]

    def test_a_card_split_apps_flat_declaration_satisfies_its_entrypoints(
        self, tmp_path: Path
    ) -> None:
        """The flat file is the app's declaration set, so declaring there clears it."""
        _write_manifest(tmp_path / "app" / "generated")
        _write_declarations(tmp_path / "app" / "generated", "raw_queries")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class CardSplitDeclaredApp(App):
                name = "card-split-declared-app"

                @entrypoint
                async def extract_metadata(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

                @entrypoint
                async def mine_queries(self, input: PlainInput) -> PlainOutput:
                    return PlainOutput()

        assert _boundary_warnings(recorded) == []

    def test_a_leftover_nested_file_never_answers_for_a_flat_app(
        self, tmp_path: Path
    ) -> None:
        """A stale subdirectory must not stand in for the flat file.

        Searching nested-first regardless of layout would let a directory left
        behind by a bundle this app used to be silently satisfy the boundary the
        flat file actually governs.
        """
        _write_manifest(tmp_path / "app" / "generated")
        _write_declarations(tmp_path / "app" / "generated" / "run", "raw_queries")

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class StaleNestedApp(App):
                name = "stale-nested-app"

                async def run(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "'raw_queries'" in messages[0]
        assert "app/generated/artifact_schemas.json" in messages[0]

    def test_an_ungenerated_tree_names_no_specific_file(self, tmp_path: Path) -> None:
        """With nothing to infer from, describe both shapes rather than guess one."""
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class UngeneratedApp(App):
                name = "ungenerated-app"

                async def run(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "for a single-entry-point app" in messages[0]
        assert "for a bundle" in messages[0]
        # Never asserts a location it cannot know.
        assert "so it lands in" not in messages[0]

    def test_the_cited_path_uses_forward_slashes_on_every_platform(self) -> None:
        """A Windows developer must be able to match the message against the docs.

        ``str(Path(...))`` renders the OS separator, so without this the same app
        reports ``app\\generated\\artifact_schemas.json`` on Windows and
        ``app/generated/artifact_schemas.json`` everywhere else — while the docs,
        the pkl contract and conformance K016 all spell it one way. Asserted
        against a ``PureWindowsPath`` so the guarantee is checked on every
        platform's CI leg, not only the Windows one.
        """
        windows_path = PureWindowsPath("app/generated/run/artifact_schemas.json")

        rendered = _Declarations(path=windows_path).display_path  # type: ignore[arg-type]

        assert rendered == "app/generated/run/artifact_schemas.json"
        assert "\\" not in rendered

    def test_the_path_cited_is_the_one_that_answered(self, tmp_path: Path) -> None:
        """When a file exists, name *that* file, not where one would belong."""
        _write_manifest(tmp_path / "app" / "generated" / "extract-metadata")
        _write_manifest(tmp_path / "app" / "generated" / "mine-queries")
        _write_declarations(
            tmp_path / "app" / "generated" / "extract-metadata", "unrelated"
        )

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class AnsweredApp(App):
                name = "answered-app"

                @entrypoint
                async def extract_metadata(self, input: DeclaredInput) -> PlainOutput:
                    return PlainOutput()

                @entrypoint
                async def mine_queries(self, input: PlainInput) -> PlainOutput:
                    return PlainOutput()

        messages = _boundary_warnings(recorded)
        assert len(messages) == 1, messages
        assert "app/generated/extract-metadata/artifact_schemas.json" in messages[0]


class TestDegradesRatherThanRaises:
    """Absent and unreadable are different answers, and neither ever raises."""

    @pytest.mark.parametrize(
        ("body", "case"),
        [
            ("{not json at all", "malformed JSON"),
            (json.dumps({"version": 1}), "envelope without a schemas object"),
            (
                json.dumps({"version": 1, "schemas": ["raw_queries"]}),
                "schemas is a list",
            ),
            (json.dumps([]), "envelope is not an object"),
        ],
    )
    def test_unreadable_declarations_report_unknown_not_undeclared(
        self, tmp_path: Path, body: str, case: str
    ) -> None:
        generated = tmp_path / "app" / "generated"
        generated.mkdir(parents=True)
        (generated / "artifact_schemas.json").write_text(body, encoding="utf-8")

        assert (
            _declared_artifact_schema_keys("run", layout="single").readable is False
        ), case

    def test_unreadable_declarations_suppress_the_whole_boundary_warning(
        self, tmp_path: Path
    ) -> None:
        """One bad JSON blob must not become a warning on every boundary field."""
        generated = tmp_path / "app" / "generated"
        generated.mkdir(parents=True)
        (generated / "artifact_schemas.json").write_text(
            "{not json at all", encoding="utf-8"
        )

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")

            class UnreadableDeclarationsApp(App):
                name = "unreadable-declarations-app"

                async def run(self, input: DeclaredInput) -> UndeclaredOutput:
                    return UndeclaredOutput()

        assert _boundary_warnings(recorded) == []

    def test_absent_generated_tree_reports_nothing_declared(self) -> None:
        result = _declared_artifact_schema_keys("run", layout="single")

        assert result.readable is True
        assert result.keys == frozenset()

    def test_a_directory_in_place_of_the_file_reports_nothing_declared(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "app" / "generated" / "artifact_schemas.json").mkdir(parents=True)

        result = _declared_artifact_schema_keys("run", layout="single")

        assert result.readable is True
        assert result.keys == frozenset()


class TestFileReferenceDetection:
    @pytest.mark.parametrize(
        "annotation",
        [
            FileReference,
            FileReference | None,
            list[FileReference],
            dict[str, FileReference],
            list[FileReference] | None,
        ],
    )
    def test_wrapped_file_references_still_count(self, annotation: object) -> None:
        """A field that can carry an artifact is a field whose artifact needs describing."""
        assert _mentions_file_reference(annotation) is True

    @pytest.mark.parametrize(
        "annotation",
        [str, int | None, list[str], dict[str, int], None, "FileReference"],
    )
    def test_non_artifact_annotations_do_not_count(self, annotation: object) -> None:
        assert _mentions_file_reference(annotation) is False

    def test_a_file_reference_subclass_counts(self) -> None:
        class TypedArtifact(FileReference):
            pass

        assert _mentions_file_reference(TypedArtifact | None) is True

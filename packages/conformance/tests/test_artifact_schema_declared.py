"""Tests for K016 EntrypointArtifactSchemaMissing.

Covers the boundary-vs-declaration cross-check: every ``FileReference`` field on
an entry point's ``input``/``return`` contract — directly, or via an inherited
base/mixin — must be keyed in that entry point's committed
``artifact_schemas.json``.  An internal ``@task`` contract is exempt.

Test helpers
------------
``_write_py``: writes ``{relative_path: source_text}`` under ``tmp_path``.
``_write_manifest``: writes the ``manifest.json`` whose presence and placement
tell :func:`scan_contract` whether the tree is ``single`` or ``multi``.
``_write_schemas``: writes an ``artifact_schemas.json`` declaring the given
contract field names, shaped like the toolkit's real output.
``_run``: writes everything, calls :func:`scan_all`, returns its findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.artifact_schema_declared import scan_all
from conformance.suite.checks.artifact_schema_declared._declarations import (
    candidate_paths,
    read_declarations,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def _write_manifest(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dag": {}}), encoding="utf-8")


def _write_schemas(path: Path, *field_names: str) -> None:
    """Write a well-formed declarations envelope keyed by *field_names*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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


_SINGLE_EP_APP = """
from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class ExtractInput(Input):
    raw_queries: FileReference | None = None


class ExtractOutput(Output):
    transformed_entities: FileReference | None = None
    row_count: int = 0


class MyApp(App):
    async def run(self, input: ExtractInput) -> ExtractOutput:
        return ExtractOutput()
"""


def _run(tmp_path: Path, py_files: dict[str, str]) -> list:
    paths = _write_py(tmp_path, py_files)
    return scan_all(paths, tmp_path)


def _fields(findings: list) -> set[str]:
    """Return the contract field names each finding names, for order-free asserts."""
    names: set[str] = set()
    for f in findings:
        # Messages read "... declares a FileReference field 'x', but ..."
        marker = "FileReference field '"
        start = f.message.index(marker) + len(marker)
        names.add(f.message[start : f.message.index("'", start)])
    return names


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k016_rule_metadata_matches_the_designed_disposition() -> None:
    rule = get_rule("K016")
    assert rule.name == "EntrypointArtifactSchemaMissing"
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.mechanism is RuleMechanism.STATIC
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.autofixable is False


# ---------------------------------------------------------------------------
# The core cross-check
# ---------------------------------------------------------------------------


def test_undeclared_boundary_fields_are_reported(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")

    findings = _run(tmp_path, {"app/main.py": _SINGLE_EP_APP})

    assert _fields(findings) == {"raw_queries", "transformed_entities"}
    assert all(f.rule_id == "K016" for f in findings)
    assert all(f.file == "app/main.py" for f in findings)


def test_declared_boundary_fields_are_silent(tmp_path: Path) -> None:
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "manifest.json")
    _write_schemas(
        generated / "artifact_schemas.json", "raw_queries", "transformed_entities"
    )

    assert _run(tmp_path, {"app/main.py": _SINGLE_EP_APP}) == []


def test_a_partially_declared_boundary_reports_only_the_gap(tmp_path: Path) -> None:
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "manifest.json")
    _write_schemas(generated / "artifact_schemas.json", "raw_queries")

    findings = _run(tmp_path, {"app/main.py": _SINGLE_EP_APP})

    assert _fields(findings) == {"transformed_entities"}
    assert "app/generated/artifact_schemas.json" in findings[0].message


def test_non_artifact_fields_are_never_reported(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")

    findings = _run(tmp_path, {"app/main.py": _SINGLE_EP_APP})

    assert "row_count" not in _fields(findings)


def test_wrapped_file_references_still_count(tmp_path: Path) -> None:
    """A field that can carry an artifact needs its artifact described."""
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    src = """
from application_sdk.app import App
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class WrappedInput(Input):
    optional_ref: FileReference | None = None
    many_refs: list[FileReference] = []
    keyed_refs: dict[str, FileReference] = {}


class PlainOutput(Output):
    row_count: int = 0


class MyApp(App):
    async def run(self, input: WrappedInput) -> PlainOutput:
        return PlainOutput()
"""

    findings = _run(tmp_path, {"app/main.py": src})

    assert _fields(findings) == {"optional_ref", "many_refs", "keyed_refs"}


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


def test_an_inherited_artifact_field_is_reported_on_the_contract_class(
    tmp_path: Path,
) -> None:
    """A field a base supplies is still on this entry point's boundary."""
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    src = """
from application_sdk.app import App
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class ArtifactCarryingBase(Input):
    inherited_ref: FileReference | None = None


class DerivedInput(ArtifactCarryingBase):
    row_count: int = 0


class PlainOutput(Output):
    row_count: int = 0


class MyApp(App):
    async def run(self, input: DerivedInput) -> PlainOutput:
        return PlainOutput()
"""

    findings = _run(tmp_path, {"app/main.py": src})

    assert _fields(findings) == {"inherited_ref"}
    # Anchored on `DerivedInput`, not on the base's line in some other file.
    assert (
        findings[0].line
        == src.splitlines().index("class DerivedInput(ArtifactCarryingBase):") + 1
    )


def test_declaring_an_inherited_artifact_field_satisfies_the_rule(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "manifest.json")
    _write_schemas(generated / "artifact_schemas.json", "inherited_ref")
    src = """
from application_sdk.app import App
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class ArtifactCarryingBase(Input):
    inherited_ref: FileReference | None = None


class DerivedInput(ArtifactCarryingBase):
    row_count: int = 0


class PlainOutput(Output):
    row_count: int = 0


class MyApp(App):
    async def run(self, input: DerivedInput) -> PlainOutput:
        return PlainOutput()
"""

    assert _run(tmp_path, {"app/main.py": src}) == []


# ---------------------------------------------------------------------------
# @task contracts are exempt
# ---------------------------------------------------------------------------


def test_task_contracts_are_exempt(tmp_path: Path) -> None:
    """Internal processing is the app's own; only the public boundary is required."""
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    src = """
from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class StageInput(Input):
    scratch_file: FileReference | None = None


class StageOutput(Output):
    scratch_file: FileReference | None = None


class PlainInput(Input):
    row_count: int = 0


class PlainOutput(Output):
    row_count: int = 0


class MyApp(App):
    @task
    async def stage(self, input: StageInput) -> StageOutput:
        return StageOutput()

    async def run(self, input: PlainInput) -> PlainOutput:
        return PlainOutput()
"""

    assert _run(tmp_path, {"app/main.py": src}) == []


# ---------------------------------------------------------------------------
# Generated-tree layouts
# ---------------------------------------------------------------------------


_BUNDLE_APP = """
from application_sdk.app import App, entrypoint
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class PlainInput(Input):
    row_count: int = 0


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


class MineOutput(Output):
    mined_queries: FileReference | None = None


class MyApp(App):
    @entrypoint
    async def extract_metadata(self, input: PlainInput) -> ExtractOutput:
        return ExtractOutput()

    @entrypoint
    async def mine_queries(self, input: PlainInput) -> MineOutput:
        return MineOutput()
"""


def test_a_bundle_reads_each_entrypoints_own_declarations(tmp_path: Path) -> None:
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "extract-metadata" / "manifest.json")
    _write_manifest(generated / "mine-queries" / "manifest.json")
    _write_schemas(
        generated / "extract-metadata" / "artifact_schemas.json", "raw_queries"
    )

    findings = _run(tmp_path, {"app/main.py": _BUNDLE_APP})

    assert _fields(findings) == {"mined_queries"}
    assert "mine-queries" in findings[0].message


def test_an_entrypoints_own_file_is_the_final_answer(tmp_path: Path) -> None:
    """The nested/flat fallback is between files, never between fields.

    ``extract-metadata`` has its own file, so the flat file's ``raw_queries``
    key must not top it up — that would satisfy one entry point's boundary with
    another scope's declarations.
    """
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "extract-metadata" / "manifest.json")
    _write_manifest(generated / "mine-queries" / "manifest.json")
    _write_schemas(
        generated / "extract-metadata" / "artifact_schemas.json", "unrelated"
    )
    _write_schemas(generated / "artifact_schemas.json", "raw_queries", "mined_queries")

    findings = _run(tmp_path, {"app/main.py": _BUNDLE_APP})

    # extract-metadata's own file omits raw_queries, so it is reported...
    assert "raw_queries" in _fields(findings)
    # ...while mine-queries, which has no file of its own, falls back to the
    # flat one, where mined_queries is declared.
    assert "mined_queries" not in _fields(findings)


# ---------------------------------------------------------------------------
# Conservative no-ops on shapes the check does not understand
# ---------------------------------------------------------------------------


def test_absent_generated_tree_is_a_noop(tmp_path: Path) -> None:
    assert _run(tmp_path, {"app/main.py": _SINGLE_EP_APP}) == []


def test_generated_tree_without_a_manifest_is_a_noop(tmp_path: Path) -> None:
    (tmp_path / "app" / "generated").mkdir(parents=True)

    assert _run(tmp_path, {"app/main.py": _SINGLE_EP_APP}) == []


def test_no_entrypoint_in_code_is_a_noop(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    src = """
from application_sdk.contracts.base import Input
from application_sdk.contracts.types import FileReference


class LooseInput(Input):
    raw_queries: FileReference | None = None
"""

    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_card_split_apps_entrypoints_check_against_the_one_flat_file(
    tmp_path: Path,
) -> None:
    """One card, several routed ``@entrypoint``s, one shared declarations file.

    Nothing is underdetermined here: the flat file is the whole app's
    declaration set, so every entrypoint's boundary is checked against it. This
    is also what the SDK's registration-time guard does, so an app is never
    warned at worker build about something this rule stayed silent on.
    """
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")

    findings = _run(tmp_path, {"app/main.py": _BUNDLE_APP})

    assert _fields(findings) == {"raw_queries", "mined_queries"}
    assert all("app/generated/artifact_schemas.json" in f.message for f in findings), [
        f.message for f in findings
    ]


def test_a_card_split_apps_flat_declaration_satisfies_every_entrypoint(
    tmp_path: Path,
) -> None:
    """Declaring in the app's one artifactSchemas block clears every entrypoint."""
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "manifest.json")
    _write_schemas(generated / "artifact_schemas.json", "raw_queries", "mined_queries")

    assert _run(tmp_path, {"app/main.py": _BUNDLE_APP}) == []


def test_an_entrypoint_with_no_bundle_subdir_is_a_noop(tmp_path: Path) -> None:
    """An unmapped entrypoint is P016's finding, not this rule's."""
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "extract-metadata" / "manifest.json")

    findings = _run(tmp_path, {"app/main.py": _BUNDLE_APP})

    assert _fields(findings) == {"raw_queries"}


def test_an_unresolvable_contract_class_is_skipped(tmp_path: Path) -> None:
    """A contract defined outside the scanned source cannot be field-resolved."""
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    src = """
from application_sdk.app import App
from elsewhere import ImportedInput, ImportedOutput


class MyApp(App):
    async def run(self, input: ImportedInput) -> ImportedOutput:
        return ImportedOutput()
"""

    assert _run(tmp_path, {"app/main.py": src}) == []


def test_unparseable_source_is_skipped(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")

    findings = _run(
        tmp_path,
        {"app/broken.py": "class Nope(:\n", "app/main.py": _SINGLE_EP_APP},
    )

    assert _fields(findings) == {"raw_queries", "transformed_entities"}


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_a_field_level_directive_suppresses_that_field_only(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    src = """
from application_sdk.app import App
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference


class ExtractInput(Input):
    raw_queries: FileReference | None = None  # conformance: ignore[K016] internal only


class ExtractOutput(Output):
    transformed_entities: FileReference | None = None


class MyApp(App):
    async def run(self, input: ExtractInput) -> ExtractOutput:
        return ExtractOutput()
"""

    findings = _run(tmp_path, {"app/main.py": src})

    unsuppressed = [f for f in findings if not f.suppressed]
    assert _fields(unsuppressed) == {"transformed_entities"}


# ---------------------------------------------------------------------------
# Declaration reading
# ---------------------------------------------------------------------------


def test_an_absent_file_means_nothing_declared(tmp_path: Path) -> None:
    (tmp_path / "app" / "generated").mkdir(parents=True)

    result = read_declarations(tmp_path, "single", None)

    assert result.status == "absent"
    assert result.keys == frozenset()
    # The finding points at where the declaration belongs.
    assert result.path == tmp_path / "app" / "generated" / "artifact_schemas.json"
    assert result.checkable is True


def test_a_malformed_file_means_unknown_not_undeclared(tmp_path: Path) -> None:
    """One bad JSON blob must not become a finding on every boundary field."""
    path = tmp_path / "app" / "generated" / "artifact_schemas.json"
    path.parent.mkdir(parents=True)

    for body in (
        "{not json at all",
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "schemas": ["raw_queries"]}),
        json.dumps([]),
    ):
        path.write_text(body, encoding="utf-8")
        result = read_declarations(tmp_path, "single", None)
        assert result.status == "unreadable", body
        assert result.checkable is False, body


def test_a_malformed_file_suppresses_the_whole_entrypoints_findings(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "app" / "generated"
    _write_manifest(generated / "manifest.json")
    (generated / "artifact_schemas.json").write_text(
        "{not json at all", encoding="utf-8"
    )

    assert _run(tmp_path, {"app/main.py": _SINGLE_EP_APP}) == []


def test_candidate_paths_are_nested_first_and_empty_for_unplaceable_shapes(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "app" / "generated"

    assert candidate_paths(tmp_path, "single", None) == [
        generated / "artifact_schemas.json"
    ]
    assert candidate_paths(tmp_path, "multi", "extract-metadata") == [
        generated / "extract-metadata" / "artifact_schemas.json",
        generated / "artifact_schemas.json",
    ]
    # multi mode needs a wire name to name the subdirectory.
    assert candidate_paths(tmp_path, "multi", None) == []
    assert candidate_paths(tmp_path, "absent", "extract-metadata") == []

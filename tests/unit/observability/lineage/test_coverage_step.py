"""The shared coverage machinery: guard, recorder, output reader, coverage step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application_sdk.observability.lineage import (
    AssetRule,
    CoverageGuard,
    CoverageRecorder,
    CoverageSpec,
    CustomSqlParserFailure,
    FieldRule,
    NoOpCoverageRecorder,
    ReasonRegistry,
    UpstreamIdentityTooSparse,
    components_hash,
    create_recorder,
    read_run_output,
    run_coverage_step,
)
from application_sdk.observability.lineage.run import (
    ASSETS_FILE,
    REFS_FILE,
    SUMMARY_FILE,
)

CONN = "default/bi/1"
REGISTRY = ReasonRegistry()
SPEC = CoverageSpec(
    app_name="bi",
    assets=(
        AssetRule("BiDataset"),
        AssetRule("BiReport", relationship_attributes=("dataset",)),
    ),
    fields=(
        FieldRule("BiField"),
        FieldRule("BiMeasure", parent_attributes=("report",)),
    ),
    sql_connectors=frozenset({"snowflake"}),
    generic_connectors=frozenset({"odbc"}),
)


def entity(type_name: str, name: str, **attributes) -> dict:
    return {
        "typeName": type_name,
        "attributes": {"qualifiedName": f"{CONN}/{name}", "name": name, **attributes},
    }


def ref(connector: str = "snowflake", table: str = "T", schema: str = "S") -> dict:
    return {
        "typeName": "Table",
        "attributes": {
            "arsIdentity": {
                "components": {
                    "connectorType": connector,
                    "databaseName": "DB",
                    "schemaName": schema,
                    "name": table,
                },
                "matchTypeNames": ["Table"],
                "noMatchAction": "drop",
            }
        },
        "uniqueAttributes": {"qualifiedName": f"{CONN}/unknown/{table}"},
    }


def link(name: str) -> dict:
    return {"typeName": "X", "uniqueAttributes": {"qualifiedName": f"{CONN}/{name}"}}


def process(name: str, output: str, inputs: list, column: bool = False) -> dict:
    return {
        "typeName": "ColumnProcess" if column else "Process",
        "attributes": {
            "qualifiedName": f"{CONN}/{name}",
            "inputs": inputs,
            "outputs": [link(output)],
        },
    }


def write(path: Path, records: list) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


@pytest.fixture
def run_files(tmp_path: Path) -> list[str]:
    assets = [
        entity("BiDataset", "pending"),
        entity("BiDataset", "internal"),
        entity("BiDataset", "nothing"),
        entity("BiDataset", "why"),
        entity("BiDataset", "generic"),
        entity("BiDataset", "file"),
        entity("BiDataset", "system"),
        entity("BiDataset", "fallback"),
        entity("BiDataset", "sparse"),
        entity("BiReport", "report", dataset={"uniqueAttributes": {}}),
        entity("BiReport", "linked"),
        entity("BiFolder", "ignored"),
        entity("BiField", "pending/f1"),
        entity("BiField", "pending/f2"),
        entity("BiField", "pending/f3"),
        entity("BiMeasure", "m1", report=link("linked")),
    ]
    relationships = [entity("BiReport", "linked", dataset=link("pending"))]
    processes = [
        process("p1", "pending", [ref(table="A"), ref(table="B")]),
        process("p2", "internal", [link("pending")]),
        process("p3", "generic", [ref("odbc")]),
        process("p4", "file", [ref("excel")]),
        process("p5", "system", [ref(schema="information_schema")]),
        process("p6", "fallback", [ref(table="FALLBACK")]),
        process("p7", "sparse", [ref(table="C")]),
        process("c1", "pending/f1", [ref(table="A")], column=True),
        process("c2", "pending/f2", [link("x")], column=True),
        process("c3", "m1", [link("pending/f1")], column=True),
        process("p9", "not-an-asset", [ref()]),
    ]
    files = [
        write(tmp_path / "t" / "assets" / "result-0.json", assets),
        write(tmp_path / "t" / "rel" / "result-0.json", relationships),
        write(tmp_path / "t" / "resolvable" / "processes-0.json", processes),
    ]
    (tmp_path / "t" / "marker.json").write_text("not json\n[1, 2]\n")
    files.append(str(tmp_path / "t" / "marker.json"))
    return files


def rows(out: Path) -> dict[str, dict]:
    lines = (out / ASSETS_FILE).read_text().splitlines()
    return {json.loads(x)["name"]: json.loads(x) for x in lines}


def test_rows_come_from_the_output_and_the_recorder(run_files, tmp_path):
    recorder = CoverageRecorder()
    recorder.missing(f"{CONN}/why", CustomSqlParserFailure, {"line": 3})
    recorder.missing(f"{CONN}/pending", CustomSqlParserFailure)  # another path built it
    recorder.unusable(f"{CONN}/fallback", CustomSqlParserFailure)
    recorder.partial(f"{CONN}/sparse", UpstreamIdentityTooSparse)
    recorder.note(f"{CONN}/pending", kind="published", skipped=None)
    recorder.field_missing(f"{CONN}/pending", "FIELD_X", 2)
    out = tmp_path / "cov"

    summary = run_coverage_step(run_files, str(out), SPEC, recorder, REGISTRY)

    assert summary["coverageStatus"] == "OK"
    got = rows(out)
    verdicts = {name: (r["verdict"], r["reasonCode"]) for name, r in got.items()}
    assert verdicts == {
        "pending": ("PENDING", None),
        "internal": ("VALID", None),
        "nothing": ("MISSING", "UNINSTRUMENTED_PATH"),
        "why": ("MISSING", "CUSTOM_SQL_PARSER_FAILURE"),
        "generic": ("MISSING", "GENERIC_CONNECTION_TYPE"),
        "file": ("VALID_NONE", "NON_SQL_SOURCE"),
        "system": ("VALID_NONE", "NON_SQL_SOURCE"),
        "fallback": ("MISSING", "CUSTOM_SQL_PARSER_FAILURE"),
        "sparse": ("PENDING", "UPSTREAM_IDENTITY_TOO_SPARSE"),
        "report": ("MISSING", "UNINSTRUMENTED_PATH"),
        "linked": ("VALID", None),
    }
    pending = got["pending"]
    assert (pending["refCount"], pending["resolvableRefCount"]) == (2, 2)
    assert pending["processQualifiedName"] == f"{CONN}/p1"
    assert (
        pending["fieldsTotal"],
        pending["fieldsWithLineage"],
        pending["fieldsPending"],
    ) == (3, 2, 1)
    assert pending["failedAttempts"] == ["CUSTOM_SQL_PARSER_FAILURE"]
    assert pending["attributes"] == {"kind": "published"}
    assert pending["fieldReasons"] == {"FIELD_X": 2}
    assert got["why"]["reasonDetails"] == {"CUSTOM_SQL_PARSER_FAILURE": {"line": 3}}
    # A field that names its parent in an attribute is counted there.
    assert (got["linked"]["fieldsTotal"], got["linked"]["fieldsWithLineage"]) == (1, 1)
    assert summary["read"]["unreadableLines"] == 1
    assert summary["read"]["processOutputsWithoutAnAsset"] == 1
    assert summary["verdicts"] == {
        "MISSING": 5,
        "PENDING": 2,
        "VALID": 2,
        "VALID_NONE": 2,
    }
    assert summary["outcome"]["lineage_assets_total"] == 11

    refs = [json.loads(x) for x in (out / REFS_FILE).read_text().splitlines()]
    mine = [r for r in refs if r["qualifiedName"] == f"{CONN}/pending"]
    assert {r["componentsHash"] for r in mine} == {
        components_hash(ref(table=t)["attributes"]["arsIdentity"]["components"])
        for t in ("A", "B")
    }
    assert [r["expected"] for r in refs if r["qualifiedName"].endswith("/file")] == [
        False
    ]


def test_an_app_can_key_its_recorder_by_its_own_id(run_files, tmp_path):
    spec = CoverageSpec(
        app_name="bi",
        assets=SPEC.assets,
        key_for=lambda type_name, qn: qn.rsplit("/", 1)[-1],
    )
    recorder = CoverageRecorder()
    recorder.missing("why", "CUSTOM_SQL_PARSER_FAILURE")
    recorder.missing("nothing", "NOT_A_KNOWN_CODE")
    run_coverage_step(run_files, str(tmp_path / "cov"), spec, recorder, REGISTRY)
    got = rows(tmp_path / "cov")
    assert got["why"]["reasonCode"] == "CUSTOM_SQL_PARSER_FAILURE"
    # An unknown code is never counted as covered, and stays visible.
    assert got["nothing"]["reasonCode"] == "UNINSTRUMENTED_PATH"
    assert got["nothing"]["recordedCode"] == "NOT_A_KNOWN_CODE"


def test_coverage_off_holds_nothing_and_still_reads_the_output(run_files, tmp_path):
    recorder = create_recorder(False)
    assert isinstance(recorder, NoOpCoverageRecorder)
    recorder.missing("k", CustomSqlParserFailure)
    recorder.note("k", a=1)
    assert recorder.get("k") is None and len(recorder) == 0
    summary = run_coverage_step(
        run_files, str(tmp_path / "cov"), SPEC, recorder, REGISTRY
    )
    assert summary["coverageStatus"] == "OK"
    assert rows(tmp_path / "cov")["why"]["reasonCode"] == "UNINSTRUMENTED_PATH"


class Boom(RuntimeError):
    pass


def boom(*args, **kwargs):
    raise Boom("injected")


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.missing("k", CustomSqlParserFailure),
        lambda r: r.partial("k", CustomSqlParserFailure),
        lambda r: r.unusable("k", CustomSqlParserFailure),
        lambda r: r.note("k", a=1),
        lambda r: r.field_missing("k", CustomSqlParserFailure),
    ],
)
def test_a_recorder_error_stops_recording_and_is_swallowed(call, monkeypatch):
    recorder = CoverageRecorder()
    monkeypatch.setattr(recorder, "_get", boom)
    assert call(recorder) is None
    assert recorder.enabled is False and recorder.error == "Boom"
    monkeypatch.undo()
    recorder.missing("later", CustomSqlParserFailure)  # off: nothing recorded
    assert recorder.get("later") is None


def test_bad_arguments_never_reach_the_caller():
    recorder = CoverageRecorder()
    recorder.missing("k", object())  # type: ignore[arg-type]
    assert recorder.enabled is False and recorder.error == "AttributeError"


def test_guard_swallows_exceptions_only():
    recorder = CoverageRecorder()
    with CoverageGuard(recorder):
        raise Boom("injected")
    assert recorder.error == "Boom"
    with CoverageGuard(None):
        raise Boom("no owner")
    with pytest.raises(KeyboardInterrupt):
        with CoverageGuard(recorder):
            raise KeyboardInterrupt


def test_a_stopped_recorder_degrades_the_run(run_files, tmp_path):
    recorder = CoverageRecorder()
    recorder.missing("k", object())  # type: ignore[arg-type]
    summary = run_coverage_step(
        run_files, str(tmp_path / "cov"), SPEC, recorder, REGISTRY
    )
    assert summary["coverageStatus"] == "DEGRADED"
    assert summary["recorderError"] == "AttributeError"
    assert len(rows(tmp_path / "cov")) == 11


class BrokenRecorder:
    error = None

    def get(self, key):
        raise Boom("injected")


@pytest.mark.parametrize(
    "case",
    ["missing_file", "recorder", "key_for", "refine", "registry", "output_dir"],
)
def test_the_step_never_raises(case, run_files, tmp_path):
    spec, recorder, registry, refine = SPEC, CoverageRecorder(), REGISTRY, None
    paths, out = list(run_files), tmp_path / "cov"
    if case == "missing_file":
        paths.append(str(tmp_path / "gone.json"))
    elif case == "recorder":
        recorder = BrokenRecorder()
    elif case == "key_for":
        spec = CoverageSpec(app_name="bi", assets=SPEC.assets, key_for=boom)
    elif case == "refine":
        refine = boom
    elif case == "registry":
        registry = None
        recorder.missing(f"{CONN}/why", "X")
    elif case == "output_dir":
        out.write_text("a file where the directory should be")

    summary = run_coverage_step(
        paths, str(out), spec, recorder, registry, refine=refine
    )

    assert summary["coverageStatus"] == "FAILED"
    assert summary["coverageError"]
    if case != "output_dir":
        assert (
            json.loads((out / SUMMARY_FILE).read_text())["coverageStatus"] == "FAILED"
        )
        assert not (out / ASSETS_FILE).exists() and not (out / REFS_FILE).exists()


def test_reading_does_not_touch_the_input(run_files):
    before = {p: Path(p).read_bytes() for p in run_files}
    list(read_run_output(run_files, SPEC))
    assert before == {p: Path(p).read_bytes() for p in run_files}

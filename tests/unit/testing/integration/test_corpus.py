"""Unit tests for the golden-corpus layout contract and loader."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from application_sdk.testing.integration._errors import (
    GoldenCorpusFormatError,
    GoldenCorpusLayoutError,
    GoldenCorpusUnavailableError,
    GoldenLayoutError,
    GoldenParquetSupportError,
)
from application_sdk.testing.integration.corpus import (
    GOLDEN_ROOT_ENV,
    GoldenCorpus,
    GoldenLayout,
    read_records,
    require_golden_corpus,
)

_RECORDS = [{"qualifiedName": "a", "typeName": "Table"}]


def _write_corpus(
    root: Path, *, stages: tuple[str, ...] = ("raw", "transformed"), tenant: str = ""
) -> Path:
    base = root / tenant if tenant else root
    for stage in stages:
        stage_dir = base / stage
        stage_dir.mkdir(parents=True)
        (stage_dir / "records.json").write_bytes(json.dumps(_RECORDS).encode())
    return root


class TestGoldenLayout:
    def test_defaults_declare_raw_as_transform_input(self) -> None:
        layout = GoldenLayout()
        assert layout.stages == ("raw", "transformed")
        assert layout.input_stage == "raw"
        assert layout.tenant_level is False

    def test_processed_input_stage_is_declarable(self) -> None:
        layout = GoldenLayout(
            stages=("raw", "processed", "transformed"), input_stage="processed"
        )
        assert layout.input_stage == "processed"

    def test_undeclared_input_stage_rejected(self) -> None:
        with pytest.raises(GoldenLayoutError):
            GoldenLayout(stages=("raw",), input_stage="processed")

    def test_empty_stages_rejected(self) -> None:
        with pytest.raises(GoldenLayoutError):
            GoldenLayout(stages=(), input_stage="raw")

    def test_duplicate_stages_rejected(self) -> None:
        with pytest.raises(GoldenLayoutError):
            GoldenLayout(stages=("raw", "raw"), input_stage="raw")

    def test_nested_stage_name_rejected(self) -> None:
        with pytest.raises(GoldenLayoutError):
            GoldenLayout(stages=("a/b",), input_stage="a/b")


class TestResolution:
    def test_env_var_wins_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chosen = _write_corpus(tmp_path / "chosen")
        ignored = _write_corpus(tmp_path / "ignored")
        monkeypatch.setenv(GOLDEN_ROOT_ENV, str(chosen))
        corpus = GoldenCorpus.from_env(default_root=ignored)
        assert corpus.root == chosen

    def test_default_root_used_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        root = _write_corpus(tmp_path / "corpus")
        assert GoldenCorpus.from_env(default_root=root).root == root

    def test_unconfigured_raises_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        with pytest.raises(GoldenCorpusUnavailableError):
            GoldenCorpus.from_env(default_root=tmp_path / "absent")

    def test_env_var_pointing_nowhere_is_a_hard_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GOLDEN_ROOT_ENV, str(tmp_path / "absent"))
        with pytest.raises(GoldenCorpusLayoutError):
            GoldenCorpus.from_env(default_root=_write_corpus(tmp_path / "corpus"))


class TestTenantAxis:
    def test_tenantless_layout_needs_no_tenant_directory(self, tmp_path: Path) -> None:
        corpus = GoldenCorpus(root=_write_corpus(tmp_path))
        assert corpus.base == tmp_path
        corpus.validate()

    def test_tenant_selection(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, tenant="tenant-a")
        _write_corpus(tmp_path, tenant="tenant-b")
        corpus = GoldenCorpus(root=tmp_path, layout=GoldenLayout(tenant_level=True))
        assert corpus.tenants() == ("tenant-a", "tenant-b")
        assert corpus.for_tenant("tenant-b").base == tmp_path / "tenant-b"

    def test_arbitrary_tenant_directory_name(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, tenant="example-demo")
        corpus = GoldenCorpus(root=tmp_path, layout=GoldenLayout(tenant_level=True))
        assert corpus.tenants() == ("example-demo",)

    def test_stage_access_without_tenant_selection_fails(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, tenant="tenant-a")
        corpus = GoldenCorpus(root=tmp_path, layout=GoldenLayout(tenant_level=True))
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.stage_dir("raw")

    def test_missing_tenant_directory_fails(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, tenant="tenant-a")
        corpus = GoldenCorpus(root=tmp_path, layout=GoldenLayout(tenant_level=True))
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.for_tenant("tenant-z")

    def test_tenant_calls_rejected_on_tenantless_layout(self, tmp_path: Path) -> None:
        corpus = GoldenCorpus(root=_write_corpus(tmp_path))
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.tenants()
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.for_tenant("tenant-a")

    def test_validate_covers_every_tenant(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, tenant="tenant-a")
        (tmp_path / "tenant-b" / "raw").mkdir(parents=True)
        corpus = GoldenCorpus(root=tmp_path, layout=GoldenLayout(tenant_level=True))
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.validate()


class TestStages:
    def test_input_dir_follows_declared_input_stage(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("raw", "processed", "transformed"))
        corpus = GoldenCorpus(
            root=tmp_path,
            layout=GoldenLayout(
                stages=("raw", "processed", "transformed"), input_stage="processed"
            ),
        )
        assert corpus.input_dir == tmp_path / "processed"

    def test_undeclared_stage_rejected(self, tmp_path: Path) -> None:
        corpus = GoldenCorpus(root=_write_corpus(tmp_path))
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.stage_dir("processed")

    def test_declared_but_absent_stage_dir_fails(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("raw",))
        corpus = GoldenCorpus(root=tmp_path)
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.stage_dir("transformed")

    def test_empty_stage_is_an_error_not_an_empty_list(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("raw",))
        (tmp_path / "transformed").mkdir()
        corpus = GoldenCorpus(root=tmp_path)
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.files("transformed")

    def test_files_found_recursively_and_sorted(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("raw", "transformed"))
        nested = tmp_path / "raw" / "Table"
        nested.mkdir()
        (nested / "chunk-0.json").write_bytes(json.dumps(_RECORDS).encode())
        corpus = GoldenCorpus(root=tmp_path)
        found = corpus.files("raw")
        assert len(found) == 2
        assert list(found) == sorted(found)

    def test_unsupported_suffixes_ignored_when_listing(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("raw", "transformed"))
        (tmp_path / "raw" / "notes.txt").write_text("ignore me")
        corpus = GoldenCorpus(root=tmp_path)
        assert [p.name for p in corpus.files("raw")] == ["records.json"]

    def test_records_that_parse_to_nothing_fail(self, tmp_path: Path) -> None:
        (tmp_path / "raw").mkdir()
        (tmp_path / "transformed").mkdir()
        (tmp_path / "raw" / "empty.json").write_bytes(b"[]")
        (tmp_path / "transformed" / "records.json").write_bytes(
            json.dumps(_RECORDS).encode()
        )
        corpus = GoldenCorpus(root=tmp_path)
        with pytest.raises(GoldenCorpusLayoutError):
            corpus.records("raw")

    def test_path_shaped_pattern_selects_one_subdir(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("extracted", "transformed"))
        tables = tmp_path / "extracted" / "tables"
        tables.mkdir()
        (tables / "0.json").write_bytes(json.dumps(_RECORDS).encode())
        reports = tmp_path / "extracted" / "reports"
        reports.mkdir()
        other_record = [{"qualifiedName": "b", "typeName": "Report"}]
        (reports / "0.json").write_bytes(json.dumps(other_record).encode())
        corpus = GoldenCorpus(
            root=tmp_path,
            layout=GoldenLayout(
                stages=("extracted", "transformed"), input_stage="extracted"
            ),
        )

        found = corpus.records("extracted", pattern="tables/*")

        assert found == _RECORDS

    def test_subdirs_returns_only_directories_sorted(self, tmp_path: Path) -> None:
        _write_corpus(tmp_path, stages=("extracted",))
        (tmp_path / "extracted" / "b_typename").mkdir()
        (tmp_path / "extracted" / "a_typename").mkdir()
        (tmp_path / "extracted" / "loose.json").write_bytes(
            json.dumps(_RECORDS).encode()
        )
        corpus = GoldenCorpus(
            root=tmp_path,
            layout=GoldenLayout(stages=("extracted",), input_stage="extracted"),
        )

        assert corpus.subdirs("extracted") == ("a_typename", "b_typename")


class TestFormats:
    def test_json_array(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        path.write_bytes(json.dumps(_RECORDS).encode())
        assert read_records(path) == _RECORDS

    def test_json_single_object(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        path.write_bytes(json.dumps(_RECORDS[0]).encode())
        assert read_records(path) == _RECORDS

    @pytest.mark.parametrize("suffix", [".ndjson", ".jsonl"])
    def test_ndjson(self, tmp_path: Path, suffix: str) -> None:
        path = tmp_path / f"a{suffix}"
        path.write_text("\n".join(json.dumps(r) for r in _RECORDS) + "\n\n")
        assert read_records(path) == _RECORDS

    def test_json_suffix_holding_ndjson(self, tmp_path: Path) -> None:
        path = tmp_path / "records.json"
        path.write_text("\n".join(json.dumps(r) for r in _RECORDS) + "\n")
        assert read_records(path) == _RECORDS

    def test_json_suffix_holding_ndjson_with_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "records.json"
        path.write_text("\n\n".join(json.dumps(r) for r in _RECORDS) + "\n")
        assert read_records(path) == _RECORDS

    def test_csv(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(_RECORDS[0]))
            writer.writeheader()
            writer.writerows(_RECORDS)
        assert read_records(path) == _RECORDS

    def test_parquet(self, tmp_path: Path) -> None:
        pa = pytest.importorskip("pyarrow")
        pq = pytest.importorskip("pyarrow.parquet")
        path = tmp_path / "a.parquet"
        pq.write_table(pa.Table.from_pylist(_RECORDS), path)
        assert read_records(path) == _RECORDS

    def test_parquet_without_pyarrow_names_the_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def _blocked(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("pyarrow"):
                raise ImportError("no pyarrow")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _blocked)
        path = tmp_path / "a.parquet"
        path.write_bytes(b"not really parquet")
        with pytest.raises(GoldenParquetSupportError) as excinfo:
            read_records(path)
        assert "[sql]" in str(excinfo.value.suggested_action)

    def test_unsupported_suffix(self, tmp_path: Path) -> None:
        path = tmp_path / "a.txt"
        path.write_text("nope")
        with pytest.raises(GoldenCorpusFormatError):
            read_records(path)

    def test_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        path.write_text("{not json")
        with pytest.raises(GoldenCorpusFormatError) as excinfo:
            read_records(path)
        assert "neither valid JSON nor valid NDJSON" in str(excinfo.value.message)

    def test_json_scalar_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        path.write_text("42")
        with pytest.raises(GoldenCorpusFormatError):
            read_records(path)

    def test_json_array_of_scalars(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        path.write_text("[1, 2]")
        with pytest.raises(GoldenCorpusFormatError):
            read_records(path)

    def test_ndjson_non_object_line(self, tmp_path: Path) -> None:
        path = tmp_path / "a.ndjson"
        path.write_text("[1]\n")
        with pytest.raises(GoldenCorpusFormatError):
            read_records(path)

    def test_ndjson_malformed_line_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "a.ndjson"
        path.write_text('{"ok": 1}\nbroken\n')
        with pytest.raises(GoldenCorpusFormatError) as excinfo:
            read_records(path)
        assert "Line 2" in str(excinfo.value.message)

    def test_csv_without_header(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_text("")
        with pytest.raises(GoldenCorpusFormatError):
            read_records(path)

    def test_records_concatenated_across_formats(self, tmp_path: Path) -> None:
        (tmp_path / "raw").mkdir()
        (tmp_path / "transformed").mkdir()
        (tmp_path / "raw" / "a.json").write_bytes(json.dumps(_RECORDS).encode())
        (tmp_path / "raw" / "b.ndjson").write_text(json.dumps(_RECORDS[0]) + "\n")
        (tmp_path / "transformed" / "c.json").write_bytes(json.dumps(_RECORDS).encode())
        corpus = GoldenCorpus(root=tmp_path)
        assert len(corpus.records("raw")) == 2


class TestRequireGoldenCorpus:
    def test_skips_when_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        with pytest.raises(pytest.skip.Exception):
            require_golden_corpus(default_root=tmp_path / "absent")

    def test_returns_validated_corpus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        root = _write_corpus(tmp_path)
        assert require_golden_corpus(default_root=root).root == root

    def test_malformed_corpus_fails_rather_than_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        _write_corpus(tmp_path, stages=("raw",))
        with pytest.raises(GoldenCorpusLayoutError):
            require_golden_corpus(default_root=tmp_path)

    def test_validation_can_be_deferred(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        _write_corpus(tmp_path, stages=("raw",))
        corpus = require_golden_corpus(default_root=tmp_path, validate=False)
        assert corpus.records("raw") == _RECORDS

    def test_tenant_selected_during_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GOLDEN_ROOT_ENV, raising=False)
        _write_corpus(tmp_path, tenant="tenant-a")
        corpus = require_golden_corpus(
            layout=GoldenLayout(tenant_level=True),
            default_root=tmp_path,
            tenant="tenant-a",
        )
        assert corpus.tenant == "tenant-a"


class TestEmptyGoldenRootEnv:
    def test_empty_env_var_is_an_error_not_a_silent_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GOLDEN_ROOT_ENV, "   ")
        with pytest.raises(GoldenCorpusLayoutError, match="set but empty"):
            GoldenCorpus.from_env(default_root=tmp_path)

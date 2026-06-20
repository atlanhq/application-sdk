"""Meta-tests for the P-series storage-seam checks (P008–P012, BLDX-1398).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations.  So each rule is tested to fire *exactly* when it
should and stay silent otherwise: both false positives and false negatives are
guarded.

Background (ADR-0014)
---------------------
Apps have two sanctioned data-movement paths:

* **Task-to-task** → ``FileReference`` on ``@task`` Input/Output contracts.
  The activity interceptor auto-uploads/downloads between workers.
* **App-to-app** → ``App.upload()`` called from ``run()``, routes to the
  upstream (Atlan) store in SDR deployments.

The rules here enforce those paths are used correctly and that apps do not
hand-stitch their own file-based durability management.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from conformance.suite.checks.prescriptions import scan_all, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope


def _rule(src: str, rule_id: str, file: str = "app/x.py") -> list:
    """Findings of a single rule from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def _scan_files(tmp_path: Path, files: dict[str, str]) -> list:
    paths: list[Path] = []
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
        paths.append(path)
    return scan_all(paths, tmp_path)


# ── Catalog invariants ──────────────────────────────────────────────────────


@pytest.mark.parametrize("rule_id", ["P008", "P009", "P010", "P011", "P012"])
def test_storage_rules_are_app_scoped(rule_id: str) -> None:
    assert get_rule(rule_id).scope is RuleScope.APP


@pytest.mark.parametrize("rule_id", ["P008", "P009", "P010", "P011", "P012"])
def test_storage_rules_are_warn_tier(rule_id: str) -> None:
    assert get_rule(rule_id).tier is EnforcementTier.WARN


@pytest.mark.parametrize("rule_id", ["P008", "P009", "P010", "P011", "P012"])
def test_storage_rules_have_category(rule_id: str) -> None:
    assert get_rule(rule_id).category == "storage-seam"


@pytest.mark.parametrize(
    "module_path,rule_id",
    [
        ("conformance.suite.checks.prescriptions._framework_transfer", "P008"),
        ("conformance.suite.checks.prescriptions._store_construction", "P009"),
        ("conformance.suite.checks.prescriptions._file_reference", "P010"),
        ("conformance.suite.checks.prescriptions._contract_fields", "P011"),
        ("conformance.suite.checks.prescriptions._contract_fields", "P012"),
    ],
)
def test_checker_module_docstring_contains_canonical_rule_name(
    module_path: str, rule_id: str
) -> None:
    mod = importlib.import_module(module_path)
    rule = get_rule(rule_id)
    assert rule.name in (
        mod.__doc__ or ""
    ), f"{module_path} docstring missing canonical rule name '{rule.name}'"


# ── P008 FrameworkTransferInsideTask ────────────────────────────────────────


def test_p008_fires_on_upload_inside_task() -> None:
    src = (
        "@task\n"
        "async def my_task(self, inp):\n"
        "    result = await self.upload(inp.ref)\n"
    )
    fs = _rule(src, "P008")
    assert len(fs) == 1
    assert fs[0].line == 3


def test_p008_fires_on_download_inside_task() -> None:
    src = (
        "@task(timeout_seconds=60)\n"
        "async def fetch(self, inp):\n"
        "    dl = await self.download(inp.key)\n"
    )
    fs = _rule(src, "P008")
    assert len(fs) == 1
    assert fs[0].line == 3


def test_p008_fires_on_attribute_task_decorator() -> None:
    # @app.task(...) form — attribute access on task
    src = (
        "@app.task(timeout_seconds=60)\n"
        "async def fetch(self, inp):\n"
        "    await self.upload(inp.ref)\n"
    )
    fs = _rule(src, "P008")
    assert len(fs) == 1
    assert fs[0].line == 3


def test_p008_fires_on_upload_in_list_comprehension_inside_task() -> None:
    # List comprehension does NOT create a nested-function scope boundary —
    # self.upload(...) inside a listcomp inside a @task is still in-task.
    src = (
        "@task\n"
        "async def my_task(self, items):\n"
        "    results = [self.upload(x) for x in items]\n"
    )
    fs = _rule(src, "P008")
    assert len(fs) == 1
    assert fs[0].line == 3


def test_p008_silent_when_upload_called_from_run() -> None:
    # App.upload() is correct when called from run() — not a @task method
    src = (
        "async def run(self, inp):\n"
        "    result = await self.upload(inp.ref)\n"
        "    return result\n"
    )
    assert _rule(src, "P008") == []


def test_p008_silent_on_non_self_upload() -> None:
    # other_obj.upload() is not an App framework call
    src = "@task\nasync def my_task(self, inp):\n    await other_obj.upload(inp.ref)\n"
    assert _rule(src, "P008") == []


def test_p008_silent_on_non_task_method() -> None:
    # A plain async def without @task is not in scope
    src = "async def helper(self, inp):\n    await self.upload(inp.ref)\n"
    assert _rule(src, "P008") == []


def test_p008_silent_nested_function_inside_task() -> None:
    # A self.upload() inside a *nested function* defined inside @task belongs
    # to the inner function, not the task — _iter_own_scope must not cross it.
    src = (
        "@task\n"
        "async def my_task(self, inp):\n"
        "    async def _inner():\n"
        "        await self.upload(inp.ref)\n"
    )
    assert _rule(src, "P008") == []


def test_p008_silent_for_non_sdk_task_name_decorator() -> None:
    # @celery.task is not the SDK's @task — import provenance excludes it
    src = (
        "import celery\n"
        "@celery.task\n"
        "def process(self, inp):\n"
        "    self.upload(inp.ref)\n"
    )
    assert _rule(src, "P008") == []


def test_p008_silent_for_explicit_non_sdk_task_import() -> None:
    # `from huey import task` re-exports task; the name must be excluded
    src = (
        "from huey import task\n"
        "@task\n"
        "def process(self, inp):\n"
        "    self.upload(inp.ref)\n"
    )
    assert _rule(src, "P008") == []


def test_p008_suppressed_inline() -> None:
    src = (
        "@task\n"
        "async def my_task(self, inp):\n"
        "    result = await self.upload(inp.ref)  # conformance: ignore[P008] legacy path before FileReference\n"
    )
    fs = _rule(src, "P008")
    assert len(fs) == 1
    assert fs[0].suppressed is True


# ── P009 ManualObjectStoreConstruction ───────────────────────────────────────


def test_p009_fires_on_import_boto3() -> None:
    src = "import boto3\n"
    fs = _rule(src, "P009")
    assert len(fs) == 1
    assert fs[0].line == 1


def test_p009_fires_on_aliased_boto3_import() -> None:
    # `import boto3 as b3` — alias doesn't hide the origin module name
    src = "import boto3 as b3\n"
    fs = _rule(src, "P009")
    assert len(fs) == 1
    assert fs[0].line == 1


def test_p009_fires_on_from_boto3_import() -> None:
    src = "from boto3 import client\n"
    fs = _rule(src, "P009")
    assert len(fs) == 1
    assert fs[0].line == 1


def test_p009_fires_on_boto3_submodule() -> None:
    src = "import boto3.session\n"
    fs = _rule(src, "P009")
    assert len(fs) == 1


def test_p009_fires_on_s3store_construction() -> None:
    src = "from obstore.store import S3Store\nstore = S3Store(bucket='my-bucket')\n"
    fs = _rule(src, "P009")
    # import is clean; only the Call is flagged
    assert any("S3Store" in f.message for f in fs)


def test_p009_fires_on_aliased_s3store_construction() -> None:
    # from obstore.store import S3Store as S3 — alias is tracked in provenance
    src = "from obstore.store import S3Store as S3\nstore = S3(bucket='my-bucket')\n"
    fs = _rule(src, "P009")
    assert any(f.rule_id == "P009" for f in fs)


def test_p009_fires_on_gcsstore_construction() -> None:
    src = "from obstore.store import GCSStore\nstore = GCSStore(bucket='my-bucket')\n"
    fs = _rule(src, "P009")
    assert any("GCSStore" in f.message for f in fs)


def test_p009_fires_on_attribute_call_obstore_form() -> None:
    # import obstore.store; obstore.store.S3Store(...) — qualified attribute call
    src = "import obstore.store\nstore = obstore.store.S3Store(bucket='my-bucket')\n"
    fs = _rule(src, "P009")
    assert len(fs) == 1
    assert fs[0].line == 2
    assert "S3Store" in fs[0].message


def test_p009_fires_on_create_store_from_binding() -> None:
    src = (
        "from application_sdk.storage import create_store_from_binding\n"
        "store = create_store_from_binding('objectstore')\n"
    )
    fs = _rule(src, "P009")
    assert any(f.rule_id == "P009" for f in fs)


def test_p009_fires_on_aliased_binding_factory() -> None:
    # Aliased import: local name tracked in provenance
    src = (
        "from application_sdk.storage import create_store_from_binding as csfb\n"
        "store = csfb('objectstore')\n"
    )
    fs = _rule(src, "P009")
    assert any(f.rule_id == "P009" for f in fs)


def test_p009_silent_on_create_memory_store() -> None:
    # create_memory_store is for local/test use — never flag it
    src = (
        "from application_sdk.storage import create_memory_store\n"
        "store = create_memory_store()\n"
    )
    assert _rule(src, "P009") == []


def test_p009_silent_on_create_local_store() -> None:
    src = (
        "from application_sdk.storage import create_local_store\n"
        "store = create_local_store('/tmp/data')\n"
    )
    assert _rule(src, "P009") == []


def test_p009_silent_on_get_infrastructure_storage() -> None:
    # Correct pattern: resolve from infra context
    src = (
        "from application_sdk.infrastructure.context import get_infrastructure\n"
        "store = get_infrastructure().storage\n"
    )
    assert _rule(src, "P009") == []


def test_p009_suppressed_inline() -> None:
    src = "import boto3  # conformance: ignore[P009] legacy direct S3 upload path\n"
    fs = _rule(src, "P009")
    assert len(fs) == 1
    assert fs[0].suppressed is True


# ── P010 ManualFileReferenceConstruction ─────────────────────────────────────


def test_p010_fires_on_storage_path_kwarg() -> None:
    src = (
        'ref = FileReference(storage_path="artifacts/my-file.json", is_durable=True)\n'
    )
    fs = _rule(src, "P010")
    assert len(fs) == 1
    assert fs[0].line == 1
    assert "storage_path" in fs[0].message


def test_p010_fires_on_is_durable_kwarg() -> None:
    src = "ref = FileReference(local_path='/tmp/f.json', is_durable=True)\n"
    fs = _rule(src, "P010")
    assert len(fs) == 1
    assert fs[0].line == 1
    assert "is_durable" in fs[0].message


def test_p010_fires_on_file_count_kwarg() -> None:
    src = "ref = FileReference(local_path='/tmp/d', file_count=5)\n"
    fs = _rule(src, "P010")
    assert len(fs) == 1
    assert "file_count" in fs[0].message


def test_p010_fires_on_multiple_managed_kwargs() -> None:
    src = "ref = FileReference(storage_path='a', is_durable=True, file_count=3)\n"
    fs = _rule(src, "P010")
    assert len(fs) == 1
    assert "storage_path" in fs[0].message
    assert "is_durable" in fs[0].message
    assert "file_count" in fs[0].message


def test_p010_fires_on_qualified_filereference() -> None:
    # types.FileReference(...) — single attribute prefix
    src = "ref = types.FileReference(storage_path='a/b', is_durable=True)\n"
    fs = _rule(src, "P010")
    assert len(fs) == 1


def test_p010_fires_on_deep_qualified_filereference() -> None:
    # sdk.types.FileReference(...) — two-level attribute prefix, attr == "FileReference"
    src = "ref = sdk.types.FileReference(storage_path='a/b', is_durable=True)\n"
    fs = _rule(src, "P010")
    assert len(fs) == 1


def test_p010_silent_on_from_local() -> None:
    # FileReference.from_local(...) is the sanctioned constructor
    src = "ref = FileReference.from_local('/tmp/output.json', tier=StorageTier.TRANSIENT)\n"
    assert _rule(src, "P010") == []


def test_p010_silent_on_safe_kwargs() -> None:
    # local_path and tier are app-managed — not flagged
    src = "ref = FileReference(local_path='/tmp/f', tier=StorageTier.TRANSIENT)\n"
    assert _rule(src, "P010") == []


def test_p010_suppressed() -> None:
    src = "ref = FileReference(storage_path='x', is_durable=True)  # conformance: ignore[P010] test fixture reconstruction\n"
    fs = _rule(src, "P010")
    assert len(fs) == 1
    assert fs[0].suppressed is True


# ── P011 RawBytesInContract ──────────────────────────────────────────────────


def test_p011_fires_on_bytes_field_in_input() -> None:
    src = "class MyInput(Input):\n    data: bytes\n"
    fs = _rule(src, "P011")
    assert len(fs) == 1
    assert fs[0].line == 2
    assert "data" in fs[0].message


def test_p011_fires_on_bytearray_field() -> None:
    # bytearray carries the same Temporal payload-size risk as bytes
    src = "class MyInput(Input):\n    blob: bytearray\n"
    fs = _rule(src, "P011")
    assert len(fs) == 1
    assert fs[0].line == 2
    assert "bytearray" in fs[0].message


def test_p011_fires_on_memoryview_field() -> None:
    src = "class MyOutput(Output):\n    view: memoryview\n"
    fs = _rule(src, "P011")
    assert len(fs) == 1
    assert "memoryview" in fs[0].message


def test_p011_fires_on_optional_bytes_in_output() -> None:
    src = "class MyOutput(Output):\n    payload: bytes | None = None\n"
    fs = _rule(src, "P011")
    assert len(fs) == 1


def test_p011_fires_on_optional_bytes_typing() -> None:
    src = (
        "from typing import Optional\n"
        "class MyInput(Input):\n"
        "    raw: Optional[bytes] = None\n"
    )
    fs = _rule(src, "P011")
    assert len(fs) == 1


def test_p011_silent_on_non_contract_class() -> None:
    # bytes field outside Input/Output is not flagged
    src = "class Config:\n    raw: bytes\n"
    assert _rule(src, "P011") == []


def test_p011_silent_on_str_field() -> None:
    # str fields are not bytes
    src = "class MyInput(Input):\n    name: str\n"
    assert _rule(src, "P011") == []


def test_p011_silent_on_filereference_field() -> None:
    src = "class MyInput(Input):\n    data: FileReference\n"
    assert _rule(src, "P011") == []


def test_p011_silent_when_output_imported_from_third_party() -> None:
    # `Output` from pydantic_ai is not the SDK contract base — must not fire
    src = "from pydantic_ai import Output\nclass MyOut(Output):\n    data: bytes\n"
    assert _rule(src, "P011") == []


def test_p011_suppressed() -> None:
    src = (
        "class MyInput(Input):\n"
        "    data: bytes  # conformance: ignore[P011] small auth token, fits in payload\n"
    )
    fs = _rule(src, "P011")
    assert len(fs) == 1
    assert fs[0].suppressed is True


# ── P012 FilePathStringInContract ────────────────────────────────────────────


def test_p012_fires_on_path_named_str_field_in_input() -> None:
    src = "class MyInput(Input):\n    output_path: str\n"
    fs = _rule(src, "P012")
    assert len(fs) == 1
    assert fs[0].line == 2
    assert "output_path" in fs[0].message


def test_p012_fires_on_file_named_field() -> None:
    src = "class MyInput(Input):\n    input_file: str\n"
    fs = _rule(src, "P012")
    assert len(fs) == 1
    assert fs[0].line == 2


def test_p012_fires_on_directory_field() -> None:
    src = "class MyOutput(Output):\n    directory: str\n"
    fs = _rule(src, "P012")
    assert len(fs) == 1


def test_p012_fires_on_optional_str_path_field() -> None:
    src = "class MyInput(Input):\n    output_dir: str | None = None\n"
    fs = _rule(src, "P012")
    assert len(fs) == 1


def test_p012_fires_via_doc_text_signal() -> None:
    # Field name is ambiguous ('source') but doc says "path to"
    src = (
        "class MyInput(Input):\n"
        '    source: str = Field(description="path to the input dump file")\n'
    )
    fs = _rule(src, "P012")
    assert len(fs) == 1
    assert fs[0].line == 2


def test_p012_fires_via_pep257_attribute_docstring() -> None:
    src = (
        "class MyInput(Input):\n"
        "    source: str\n"
        '    """Local path to the CSV export."""\n'
    )
    fs = _rule(src, "P012")
    assert len(fs) == 1
    assert fs[0].line == 2


def test_p012_field_description_and_pep257_each_fire_once() -> None:
    # Both Field(description=) and a PEP-257 docstring are present on the same
    # field — the finding must still be emitted exactly once (no duplication).
    src = (
        "class MyInput(Input):\n"
        '    source: str = Field(description="path to the dump file")\n'
        '    """Local path to the CSV export."""\n'
    )
    fs = _rule(src, "P012")
    assert len(fs) == 1


def test_p012_fires_via_json_extension_in_doc() -> None:
    src = (
        "class MyInput(Input):\n"
        '    data_source: str = Field(description="location of the .json schema file")\n'
    )
    fs = _rule(src, "P012")
    assert len(fs) == 1


def test_p012_silent_on_url_field() -> None:
    # 'url' does not match the path-name pattern and typical doc text
    src = "class MyInput(Input):\n    endpoint_url: str\n"
    assert _rule(src, "P012") == []


def test_p012_silent_on_name_field() -> None:
    src = "class MyInput(Input):\n    app_name: str\n"
    assert _rule(src, "P012") == []


def test_p012_silent_on_non_str_path_annotation() -> None:
    # Path-named field but annotated FileReference — not a string, don't flag
    src = "class MyInput(Input):\n    output_path: FileReference\n"
    assert _rule(src, "P012") == []


def test_p012_silent_on_non_contract_class() -> None:
    # path-named str field outside Input/Output is not flagged
    src = "class Config:\n    output_path: str\n"
    assert _rule(src, "P012") == []


def test_p012_silent_ambiguous_field_no_doc_signal() -> None:
    # 'source' has no path doc — not flagged by either signal
    src = "class MyInput(Input):\n    source: str\n"
    assert _rule(src, "P012") == []


def test_p012_silent_when_output_imported_from_third_party() -> None:
    # `Output` from strawberry is not the SDK contract base — must not fire
    src = (
        "from strawberry import type as strawberry_type\n"
        "from strawberry_django.types import Output\n"
        "class MyOut(Output):\n"
        "    output_path: str\n"
    )
    assert _rule(src, "P012") == []


def test_p012_suppressed() -> None:
    src = (
        "class MyInput(Input):\n"
        "    output_path: str  # conformance: ignore[P012] always a GCS URI not a local path\n"
    )
    fs = _rule(src, "P012")
    assert len(fs) == 1
    assert fs[0].suppressed is True

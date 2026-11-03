"""Tests for P060 UploadedFileNotDeleted.

The rule fires on an SDK ``upload_file`` / ``upload_prefix`` whose local source
stays on disk: no ``retain_local_copy=False`` and no removal of the same path
after the upload.  It is BLOCK and ``/remediate`` applies its fix, so the
negatives matter as much as the positives: every shape of "already removed"
(later, in a ``finally``, through an offload, through a helper, of the parent
directory) and every lookalike call (an app helper of the same name, a client
method, the v2 classmethod) must stay silent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite import runner
from conformance.suite.checks._local_files import SDK_STORAGE_MODULES
from conformance.suite.checks.worker_disk import scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_HEAD = (
    "import asyncio\n"
    "import os\n"
    "import shutil\n"
    "from pathlib import Path\n"
    "from application_sdk.storage import upload_file\n"
    "from application_sdk.storage.batch import upload_prefix\n"
    "from application_sdk.contracts.types import FileReference\n\n"
)
_FIRST = _HEAD.count("\n") + 1  # line of the first statement after the header


def _p060(body: str, *, head: str = _HEAD, file: str = "app/x.py") -> list[int]:
    """Lines of the P060 findings in *head* + *body*, relative to *body*."""
    offset = head.count("\n")
    findings = scan_text(head + body, file)
    return [f.line - offset for f in findings if f.rule_id == "P060"]


def _messages(body: str) -> list[str]:
    return [
        f.message for f in scan_text(_HEAD + body, "app/x.py") if f.rule_id == "P060"
    ]


# ── fires ──────────────────────────────────────────────────────────────────────


def test_fires_when_the_file_is_never_removed() -> None:
    assert _p060("async def f(p):\n    await upload_file('k', p)\n") == [2]


def test_fires_on_literal_true_without_a_removal() -> None:
    body = "async def f(p):\n    await upload_file('k', p, retain_local_copy=True)\n"
    assert _p060(body) == [2]
    assert "it passes retain_local_copy=True" in _messages(body)[0]


def test_fires_on_upload_prefix_without_rmtree() -> None:
    body = "async def f(out_dir):\n    await upload_prefix(out_dir, 'dest')\n"
    assert _p060(body) == [2]
    assert "shutil.rmtree(out_dir, ignore_errors=True)" in _messages(body)[0]


@pytest.mark.parametrize("module", sorted(SDK_STORAGE_MODULES["upload_file"]))
def test_fires_for_every_module_that_exports_upload_file(module: str) -> None:
    head = f"from {module} import upload_file\n\n"
    assert _p060("async def f(p):\n    await upload_file('k', p)\n", head=head) == [2]


def test_fires_through_aliases_module_aliases_and_dotted_paths() -> None:
    head = (
        "import application_sdk.storage.ops\n"
        "import application_sdk.storage.batch as batch\n"
        "from application_sdk.storage.ops import upload_file as storage_upload\n\n"
    )
    body = (
        "async def f(p, d):\n"
        "    await storage_upload('k', local_path=p)\n"
        "    await batch.upload_prefix(d, 'dest')\n"
        "    await application_sdk.storage.ops.upload_file('k', p)\n"
    )
    assert _p060(body, head=head) == [2, 3, 4]


def test_a_data_value_read_from_the_file_is_not_the_file() -> None:
    # Returning a row count computed from the file does not hand the file off.
    body = (
        "async def f(local):\n"
        "    rows = count_rows(local)\n"
        "    await upload_file('k', local)\n"
        "    return {'rows': rows}\n"
    )
    assert _p060(body) == [3]


def test_message_names_a_later_read() -> None:
    body = (
        "async def f(p):\n"
        "    await upload_file('k', p)\n"
        "    size = os.path.getsize(p)\n"
        "    return size\n"
    )
    assert _p060(body) == [2]
    assert f"read again at line {_FIRST + 2}" in _messages(body)[0]


def test_message_names_a_parameter_source() -> None:
    body = "async def f(local_path):\n    await upload_file('k', local_path)\n"
    assert "parameter local_path" in _messages(body)[0]


def test_fires_when_the_removal_precedes_the_upload() -> None:
    body = "async def f(p):\n" "    os.remove(p)\n" "    await upload_file('k', p)\n"
    assert _p060(body) == [3]


# ── silent: removed after the upload ───────────────────────────────────────────


@pytest.mark.parametrize(
    "removal",
    [
        "os.remove(p)",
        "os.unlink(p)",
        "Path(p).unlink(missing_ok=True)",
        "delete_local_files([p])",
        "await asyncio.to_thread(os.remove, p)",
    ],
)
def test_silent_when_the_file_is_removed_later(removal: str) -> None:
    body = f"async def f(p):\n    await upload_file('k', p)\n    {removal}\n"
    assert _p060(body) == []


def test_silent_when_removed_in_a_finally() -> None:
    body = (
        "async def f(p):\n"
        "    try:\n"
        "        await upload_file('k', p)\n"
        "    finally:\n"
        "        os.remove(p)\n"
    )
    assert _p060(body) == []


def test_silent_when_removed_in_except_and_else() -> None:
    body = (
        "async def f(p):\n"
        "    try:\n"
        "        await upload_file('k', p)\n"
        "    except Exception:\n"
        "        os.unlink(p)\n"
        "        raise\n"
        "    else:\n"
        "        os.unlink(p)\n"
    )
    assert _p060(body) == []


def test_silent_when_the_parent_directory_is_removed() -> None:
    body = (
        "async def f(out_dir):\n"
        "    for name in os.listdir(out_dir):\n"
        "        await upload_file(name, os.path.join(out_dir, name))\n"
        "    shutil.rmtree(out_dir, ignore_errors=True)\n"
    )
    assert _p060(body) == []


def test_silent_on_the_upload_prefix_then_rmtree_slice_loop() -> None:
    # One bounded slice at a time: upload the directory, remove it, next slice.
    body = (
        "async def f(slices, out_dir):\n"
        "    for index, part in enumerate(slices):\n"
        "        write(part, out_dir)\n"
        "        await upload_prefix(out_dir, out_dir, prune=index == 0)\n"
        "        await asyncio.to_thread(shutil.rmtree, out_dir, ignore_errors=True)\n"
    )
    assert _p060(body) == []


def test_silent_when_an_alias_is_removed() -> None:
    body = (
        "async def f(p):\n"
        "    staged = Path(p)\n"
        "    await upload_file('k', p)\n"
        "    staged.unlink(missing_ok=True)\n"
    )
    assert _p060(body) == []


def test_silent_when_a_module_helper_removes_it() -> None:
    body = (
        "def _drop(path):\n"
        "    os.remove(path)\n\n"
        "async def f(p):\n"
        "    await upload_file('k', p)\n"
        "    _drop(p)\n"
    )
    assert _p060(body) == []


def test_silent_inside_a_temporary_directory_block() -> None:
    head = _HEAD + "import tempfile\n\n"
    body = (
        "async def f(rows):\n"
        "    with tempfile.TemporaryDirectory(dir='/data') as tmp:\n"
        "        path = os.path.join(tmp, 'out.json')\n"
        "        write(rows, path)\n"
        "        await upload_file('k', path)\n"
    )
    assert _p060(body, head=head) == []


def test_silent_with_retain_local_copy_false() -> None:
    body = "async def f(p):\n    await upload_file('k', p, retain_local_copy=False)\n"
    assert _p060(body) == []


@pytest.mark.parametrize("expr", ["keep", "self.keep", "not cleanup", "flag()"])
def test_silent_on_a_non_literal_retain_expression(expr: str) -> None:
    body = (
        "async def f(self, p, keep, cleanup, flag):\n"
        f"    await upload_file('k', p, retain_local_copy={expr})\n"
    )
    assert _p060(body) == []


def test_silent_when_a_kwargs_splat_may_carry_the_keyword() -> None:
    assert _p060("async def f(p, opts):\n    await upload_file('k', p, **opts)\n") == []


# ── silent: the path leaves the function ───────────────────────────────────────


def test_silent_when_the_file_reference_is_returned() -> None:
    body = (
        "async def f(p):\n"
        "    await upload_file('k', p)\n"
        "    return FileReference.from_local(p)\n"
    )
    assert _p060(body) == []


def test_silent_when_a_reference_to_the_parent_directory_is_returned() -> None:
    body = (
        "async def f(local_dir):\n"
        "    files = [p for p in Path(local_dir).rglob('*') if p.is_file()]\n"
        "    for file_path in files:\n"
        "        await upload_file(str(file_path), file_path)\n"
        "    return FileReference(local_path=local_dir, file_count=len(files))\n"
    )
    assert _p060(body) == []


def test_silent_when_the_path_is_stored_on_self() -> None:
    body = "async def f(self, p):\n    await upload_file('k', p)\n    self.last = p\n"
    assert _p060(body) == []


# ── silent: import resolution negatives ────────────────────────────────────────


def test_silent_on_an_app_defined_upload_file() -> None:
    head = "import os\n\n"
    body = (
        "async def upload_file(key, path):\n    return key\n\n"
        "async def f(p):\n    await upload_file('k', p)\n"
    )
    assert _p060(body, head=head) == []


@pytest.mark.parametrize(
    "imp",
    [
        "from app.storage import upload_file",
        "from .storage import upload_file",
        # transfer.py imports upload_file inside a function, so it exports nothing.
        "from application_sdk.storage.transfer import upload_file",
    ],
)
def test_silent_when_upload_file_comes_from_elsewhere(imp: str) -> None:
    head = f"{imp}\n\n"
    assert _p060("async def f(p):\n    await upload_file('k', p)\n", head=head) == []


def test_silent_on_method_calls_and_the_v2_classmethod() -> None:
    body = (
        "async def f(self, client, p):\n"
        "    await client.upload_file('k', p)\n"
        "    await self.upload_file('k', p)\n"
        "    ObjectStore.upload_file(source=p, destination='k')\n"
    )
    assert _p060(body) == []


def test_silent_on_sibling_sdk_helpers() -> None:
    head = "from application_sdk.storage import upload_file_from_bytes\n\n"
    body = "async def f(b):\n    await upload_file_from_bytes('k', b)\n"
    assert _p060(body, head=head) == []


def test_silent_under_a_developer_tooling_directory() -> None:
    body = "async def f(p):\n    await upload_file('k', p)\n"
    assert _p060(body, file="scripts/bench.py") == []
    assert _p060(body, file="app/scripts/upload.py") == [2]


# ── suppression, metadata, scope ───────────────────────────────────────────────


def test_suppressed_by_a_justified_directive() -> None:
    body = (
        "async def f(p):\n"
        "    # conformance: ignore[P060] run() re-uploads it to the upstream store\n"
        "    await upload_file('k', p)\n"
    )
    findings = [f for f in scan_text(_HEAD + body, "app/x.py") if f.rule_id == "P060"]
    assert [f.suppressed for f in findings] == [True]


def test_rule_metadata() -> None:
    rule = get_rule("P060")
    assert rule.name == "UploadedFileNotDeleted"
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is True
    assert "Customer impact:" in rule.rationale
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.PRESCRIPTION
    assert rule.remediation_reference.target == "programs/areas/prescriptions.prose.md"


def _repo(tmp_path: Path, project: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "io.py").write_text(
        _HEAD + "async def f(p):\n    await upload_file('k', p)\n", encoding="utf-8"
    )
    return tmp_path


def _run(repo: Path) -> tuple[int, list[str]]:
    out = repo / "out.sarif"
    code = runner.main(["--repo", str(repo), "--rule", "P060", "--output", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return code, [r["ruleId"] for r in doc["runs"][0]["results"]]


def test_runner_reports_p060_as_blocking_on_an_app_repo(tmp_path: Path) -> None:
    code, ids = _run(_repo(tmp_path, "atlan-demo-app"))
    assert ids == ["P060"]
    assert code != 0  # BLOCK: the gate fails unless the repo runs with --exit-zero


def test_runner_drops_p060_on_the_sdk(tmp_path: Path) -> None:
    code, ids = _run(_repo(tmp_path, "atlan-application-sdk"))
    assert ids == []
    assert code == 0

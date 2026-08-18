"""P044 DirectStoragePrefixTransfer.

The rule closes a gap two existing rules each miss for a structural reason, so
the negative cases below matter as much as the positive ones: P009 does not fire
because the app calls a real SDK function, and P030/P042 do not fire because they
are gated on ``self_deployed_runtime``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks.prescriptions import check_p044, scan_path


def _check(src: str, filename: str = "app/io.py") -> list:
    return check_p044(ast.parse(src), filename, _parse_directives(src))


# ── positive ────────────────────────────────────────────────────────────────


def test_flags_direct_import_form() -> None:
    findings = _check(
        "from application_sdk.storage import upload_prefix\n"
        "\n"
        "async def go(local, prefix):\n"
        "    await upload_prefix(local, prefix)\n"
    )
    assert [f.rule_id for f in findings] == ["P044"]
    assert findings[0].line == 4
    assert "upload_prefix()" in findings[0].message


def test_flags_download_and_names_the_matching_app_method() -> None:
    """The hint has to name App.download() for a download, not App.upload()."""
    findings = _check(
        "from application_sdk.storage import download_prefix\n"
        "\n"
        "async def go(prefix, d):\n"
        "    await download_prefix(prefix=prefix, local_dir=d)\n"
    )
    assert len(findings) == 1
    assert "App.download()" in findings[0].message


def test_flags_module_attribute_form() -> None:
    findings = _check(
        "from application_sdk import storage\n"
        "\n"
        "async def go(local, prefix):\n"
        "    await storage.upload_prefix(local, prefix)\n"
    )
    assert len(findings) == 1


def test_flags_batch_module_plain_import() -> None:
    """The prefix helpers live in ``application_sdk/storage/batch.py``, so this
    is the most natural module form — the gate resolves the import *source*, so
    a ``batch`` binding must register even though its name is neither ``storage``
    nor ``*ops``."""
    findings = _check(
        "from application_sdk.storage import batch\n"
        "\n"
        "async def go(local, prefix):\n"
        "    await batch.upload_prefix(local, prefix)\n"
    )
    assert len(findings) == 1


def test_flags_batch_module_aliased_as_ops() -> None:
    """``batch as ops`` reaches the same helpers through a renamed module
    binding; the docstring's attribute form (``ops.download_prefix(...)``) only
    holds if the gate keys on the import source rather than the alias text."""
    findings = _check(
        "from application_sdk.storage import batch as ops\n"
        "\n"
        "async def go(prefix, d):\n"
        "    await ops.download_prefix(prefix, d)\n"
    )
    assert len(findings) == 1


def test_flags_dotted_import_form() -> None:
    findings = _check(
        "import application_sdk.storage\n"
        "\n"
        "async def go(local, prefix):\n"
        "    await application_sdk.storage.upload_prefix(local, prefix)\n"
    )
    assert len(findings) == 1


def test_flags_dotted_batch_import_aliased() -> None:
    """``import application_sdk.storage.batch as ops`` binds the defining module
    under a free name — the Import branch has to resolve it by path, exactly as
    the ImportFrom branch does."""
    findings = _check(
        "import application_sdk.storage.batch as ops\n"
        "\n"
        "async def go(local, prefix):\n"
        "    await ops.upload_prefix(local, prefix)\n"
    )
    assert len(findings) == 1


def test_flags_dotted_batch_import_plain() -> None:
    """The plain dotted form binds the root package, so the call site spells the
    whole path back out."""
    findings = _check(
        "import application_sdk.storage.batch\n"
        "\n"
        "async def go(prefix, d):\n"
        "    await application_sdk.storage.batch.download_prefix(prefix, d)\n"
    )
    assert len(findings) == 1


def test_flags_aliased_import() -> None:
    findings = _check(
        "from application_sdk.storage import upload_prefix as push\n"
        "\n"
        "async def go(local, prefix):\n"
        "    await push(local, prefix)\n"
    )
    assert len(findings) == 1


def test_flags_a_lazy_import_inside_a_function() -> None:
    """The connector that motivated this rule imports inside the function body
    to avoid a module-level cost, so a module-level-only scan would miss it."""
    findings = _check(
        "async def go(local, prefix):\n"
        "    from application_sdk.storage import upload_prefix\n"
        "\n"
        "    await upload_prefix(local, prefix)\n"
    )
    assert len(findings) == 1


def test_reports_every_call_site() -> None:
    findings = _check(
        "from application_sdk.storage import upload_prefix\n"
        "\n"
        "async def a(x, y):\n"
        "    await upload_prefix(x, y)\n"
        "\n"
        "async def b(x, y):\n"
        "    await upload_prefix(x, y)\n"
    )
    assert [f.line for f in findings] == [4, 7]


# ── negative ────────────────────────────────────────────────────────────────


def test_same_named_local_helper_is_not_flagged() -> None:
    """The name must resolve to an application_sdk import in the same file — a
    local helper that happens to share the name is a different function."""
    findings = _check(
        "def upload_prefix(local, prefix):\n"
        "    return None\n"
        "\n"
        "def go(x, y):\n"
        "    return upload_prefix(x, y)\n"
    )
    assert findings == []


def test_single_object_transfers_are_out_of_scope() -> None:
    """upload_file/download_file are the right tool when the caller holds one
    file and has no contract boundary to hang a reference on."""
    findings = _check(
        "from application_sdk.storage import download_file, upload_file\n"
        "\n"
        "async def go(key, path):\n"
        "    await upload_file(key, path)\n"
        "    await download_file(key, path)\n"
    )
    assert findings == []


def test_app_upload_is_not_flagged() -> None:
    """The sanctioned shape must be silent, or the rule contradicts its own fix."""
    findings = _check(
        "from application_sdk.contracts.storage import UploadInput\n"
        "\n"
        "class App:\n"
        "    async def run(self, d):\n"
        "        await self.upload(UploadInput(local_path=d))\n"
    )
    assert findings == []


def test_same_named_helper_from_another_sdk_subpackage_is_not_flagged() -> None:
    """The gate is the import *path*, not the ``application_sdk`` prefix: only
    ``application_sdk.storage`` and ``application_sdk.storage.batch`` expose these
    helpers, so a same-named symbol from anywhere else in the SDK is a different
    function."""
    findings = _check(
        "from application_sdk.contracts.types import upload_prefix\n"
        "\n"
        "async def go(x, y):\n"
        "    await upload_prefix(x, y)\n"
    )
    assert findings == []


def test_same_named_module_from_another_sdk_subpackage_is_not_flagged() -> None:
    """``from application_sdk.contracts import storage`` binds the alias text the
    attribute form looks for, from a module that does not have the helpers."""
    findings = _check(
        "from application_sdk.contracts import storage\n"
        "\n"
        "async def go(x, y):\n"
        "    await storage.upload_prefix(x, y)\n"
    )
    assert findings == []


def test_unrelated_storage_submodule_alias_is_not_flagged() -> None:
    """``application_sdk.storage.formats.parquet`` is under the storage package
    but is not one of the two modules that re-export or define the helpers."""
    findings = _check(
        "import application_sdk.storage.formats.parquet as pq\n"
        "\n"
        "async def go(x, y):\n"
        "    await pq.upload_prefix(x, y)\n"
    )
    assert findings == []


def test_unrelated_sdk_import_does_not_license_a_dotted_call() -> None:
    """Importing some other part of the SDK must not register the root package as
    a receiver — otherwise any ``application_sdk.*.upload_prefix(...)`` matches."""
    findings = _check(
        "import application_sdk.contracts\n"
        "\n"
        "async def go(x, y):\n"
        "    await application_sdk.storage.upload_prefix(x, y)\n"
    )
    assert findings == []


def test_third_party_prefix_helper_is_not_flagged() -> None:
    findings = _check(
        "from some_other_lib.storage import upload_prefix\n"
        "\n"
        "async def go(x, y):\n"
        "    await upload_prefix(x, y)\n"
    )
    assert findings == []


def test_inline_suppression_is_honoured_and_stays_visible() -> None:
    findings = _check(
        "from application_sdk.storage import upload_prefix\n"
        "\n"
        "async def go(local, prefix):\n"
        "    # conformance: ignore[P044] wholesale state-dir sync, no contract boundary\n"
        "    await upload_prefix(local, prefix)\n"
    )
    # Suppressed, not dropped: it still reaches SARIF in its own category.
    assert len(findings) == 1
    assert findings[0].suppressed


# ── interaction with the rules it complements ───────────────────────────────


def test_does_not_co_fire_with_p008_on_one_site(tmp_path: Path) -> None:
    """P008 flags self.upload()/self.download() inside a @task; P044 flags the
    prefix primitives. Disjoint by subject, so one call site can never be both —
    which matters because their fixes pull in opposite directions (P008 says move
    the transfer out of the task, P044 says adopt the very method P008 guards)."""
    src = (
        "from application_sdk.app import task\n"
        "from application_sdk.storage import upload_prefix\n"
        "\n"
        "class A:\n"
        "    @task()\n"
        "    async def t(self, d, prefix):\n"
        "        await upload_prefix(d, prefix)\n"
        "        await self.upload(d)\n"
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "my-connector"\n', encoding="utf-8")
    target = tmp_path / "app" / "io.py"
    target.parent.mkdir(parents=True)
    target.write_text(src, encoding="utf-8")

    by_rule: dict[str, list[int]] = {}
    for f in scan_path(target, tmp_path):
        by_rule.setdefault(f.rule_id, []).append(f.line)

    # The prefix call is P044's alone; the self.upload() call is P008's alone.
    assert by_rule.get("P044") == [7]
    assert by_rule.get("P008") == [8]


def test_p009_stays_silent_on_the_sanctioned_seam() -> None:
    """The reason this rule had to exist: P009 fires on an app constructing its
    own store, and calling an SDK function is not that."""
    from conformance.suite.checks.prescriptions import check_p009

    src = (
        "from application_sdk.storage import upload_prefix\n"
        "\n"
        "async def go(x, y):\n"
        "    await upload_prefix(x, y)\n"
    )
    tree = ast.parse(src)
    directives = _parse_directives(src)
    assert check_p009(tree, "app/io.py", directives) == []
    assert len(check_p044(tree, "app/io.py", directives)) == 1

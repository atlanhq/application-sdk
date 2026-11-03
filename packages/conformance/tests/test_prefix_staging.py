"""Tests for O010 WholePrefixStagedOnDisk.

The rule is about staging, not copying: it fires when ``download_prefix`` fills a
local directory with a whole prefix and ``upload_prefix`` then re-uploads that
tree, so every object sits on the worker's disk at once.  A move across stores is
expected and is not graded on its own; a per-file move, a per-batch loop that
removes each batch, and the SDK's own cross-store hand-off (``App.upload`` and
the ``application_sdk.storage.transfer`` helpers) must all stay silent.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite import runner
from conformance.suite.checks.optimizations import scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_HEAD = (
    "import os\n"
    "import shutil\n"
    "from pathlib import Path\n"
    "from application_sdk.storage import (\n"
    "    download_file,\n"
    "    download_prefix,\n"
    "    upload_file,\n"
    "    upload_prefix,\n"
    ")\n\n"
)
_OFFSET = _HEAD.count("\n")


def _o010(body: str, *, head: str = _HEAD, file: str = "app/x.py") -> list[int]:
    offset = head.count("\n")
    return [
        f.line - offset for f in scan_text(head + body, file) if f.rule_id == "O010"
    ]


def _message(body: str) -> str:
    (finding,) = [f for f in scan_text(_HEAD + body, "app/x.py") if f.rule_id == "O010"]
    return finding.message


# ── fires ──────────────────────────────────────────────────────────────────────


def test_fires_on_a_whole_prefix_staged_and_re_uploaded() -> None:
    body = (
        "async def copy(src, local, dst):\n"
        "    await download_prefix(src, local)\n"
        "    await upload_prefix(local, dst)\n"
    )
    assert _o010(body) == [3]
    message = _message(body)
    assert f"download_prefix() at line {_OFFSET + 2}" in message
    for needle in (
        "bounded batches",
        "256 MiB",
        "max_concurrency=4",
        "about 4 in flight",
    ):
        assert needle in message
    assert "same store" in message  # neither call passes store=


def test_cross_store_staging_still_fires_but_never_suggests_an_in_store_copy() -> None:
    body = (
        "async def promote(src, local, dst, upstream):\n"
        "    await download_prefix(src, local)\n"
        "    await upload_prefix(local, dst, store=upstream)\n"
    )
    assert _o010(body) == [3]
    message = _message(body)
    assert "Moving across stores through the worker is expected" in message
    assert "inside the store" not in message


def test_fires_when_the_upload_root_is_under_the_download_dir() -> None:
    body = (
        "async def save(src_prefix, tmp_dir, dest):\n"
        "    await download_prefix(src_prefix, Path(tmp_dir))\n"
        "    upload_root = Path(tmp_dir) / src_prefix.strip('/')\n"
        "    await upload_prefix(upload_root, dest)\n"
    )
    assert _o010(body) == [4]


def test_fires_on_a_loop_that_never_removes_its_batch() -> None:
    body = (
        "async def copy(prefixes, local, dst):\n"
        "    for prefix in prefixes:\n"
        "        await download_prefix(prefix, local)\n"
        "        await upload_prefix(local, dst)\n"
    )
    assert _o010(body) == [4]


# ── silent ─────────────────────────────────────────────────────────────────────


def test_silent_on_a_per_batch_loop_that_removes_each_batch() -> None:
    body = (
        "async def copy(batches, local, dst):\n"
        "    for batch in batches:\n"
        "        await download_prefix(batch, local)\n"
        "        await upload_prefix(local, dst)\n"
        "        shutil.rmtree(local, ignore_errors=True)\n"
    )
    assert _o010(body) == []


def test_silent_on_a_per_file_loop() -> None:
    body = (
        "async def move(keys, tmp, upstream):\n"
        "    for key in keys:\n"
        "        await download_file(key, tmp)\n"
        "        await upload_file(key, tmp, store=upstream)\n"
        "        os.remove(tmp)\n"
    )
    assert _o010(body) == []


def test_silent_on_the_sdk_cross_store_hand_off() -> None:
    head = _HEAD + "from application_sdk.storage.transfer import upload\n\n"
    body = (
        "async def handoff(self, src, local):\n"
        "    await download_prefix(src, local)\n"
        "    await self.upload(UploadInput(local_path=local))\n"
        "    await upload(local, storage_path='artifacts/x')\n"
    )
    assert _o010(body, head=head) == []


def test_silent_when_the_upload_is_an_unrelated_directory() -> None:
    body = (
        "async def transform(src, base, dst):\n"
        "    local_in = os.path.join(base, 'in')\n"
        "    local_out = os.path.join(base, 'out')\n"
        "    await download_prefix(src, local_in)\n"
        "    convert(local_in, local_out)\n"
        "    await upload_prefix(local_out, dst)\n"
    )
    assert _o010(body) == []


def test_silent_when_the_upload_comes_first() -> None:
    body = (
        "async def roundtrip(local, prefix):\n"
        "    await upload_prefix(local, prefix)\n"
        "    await download_prefix(prefix, local)\n"
    )
    assert _o010(body) == []


def test_silent_on_lookalike_helpers() -> None:
    head = "from app.storage import download_prefix, upload_prefix\n\n"
    body = (
        "async def copy(src, local, dst):\n"
        "    await download_prefix(src, local)\n"
        "    await upload_prefix(local, dst)\n"
    )
    assert _o010(body, head=head) == []


def test_silent_under_a_developer_tooling_directory() -> None:
    body = (
        "async def copy(src, local, dst):\n"
        "    await download_prefix(src, local)\n"
        "    await upload_prefix(local, dst)\n"
    )
    assert _o010(body, file="scripts/migrate.py") == []


# ── suppression, metadata, scope ───────────────────────────────────────────────


def test_suppressed_by_a_justified_directive() -> None:
    body = (
        "async def baseline(src, local, dst):\n"
        "    await download_prefix(src, local)\n"
        "    build_baseline(local)\n"
        "    # conformance: ignore[O010] the baseline build needs every file at once\n"
        "    await upload_prefix(local, dst)\n"
    )
    findings = [f for f in scan_text(_HEAD + body, "app/x.py") if f.rule_id == "O010"]
    assert [f.suppressed for f in findings] == [True]


def test_rule_metadata() -> None:
    rule = get_rule("O010")
    assert rule.name == "WholePrefixStagedOnDisk"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is False
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.GUIDE
    assert rule.remediation_reference.target == "programs/areas/optimizations.prose.md"
    # The rule text says outright that cross-store moves are expected.
    assert "moving data across stores, which the SDK expects" in " ".join(
        rule.full_description.split()
    )


def _repo(tmp_path: Path, project: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "copy.py").write_text(
        _HEAD + "async def copy(src, local, dst):\n"
        "    await download_prefix(src, local)\n"
        "    await upload_prefix(local, dst)\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(repo: Path) -> tuple[int, list[str]]:
    out = repo / "out.sarif"
    code = runner.main(["--repo", str(repo), "--rule", "O010", "--output", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return code, [r["ruleId"] for r in doc["runs"][0]["results"]]


def test_runner_reports_o010_as_a_warning(tmp_path: Path) -> None:
    code, ids = _run(_repo(tmp_path, "atlan-demo-app"))
    assert ids == ["O010"]
    assert code == 0  # WARN never fails the gate


def test_runner_drops_o010_on_the_sdk(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path, "atlan-application-sdk")) == (0, [])

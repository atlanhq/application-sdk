"""Entry-point resilience: one non-UTF-8 file must never crash a scan.

``read_text(encoding="utf-8")`` raises ``UnicodeDecodeError``, which is a
``ValueError`` and **not** an ``OSError`` — so an ``OSError``-only guard lets it
escape.  ``runner.py`` wraps neither ``discover()`` nor ``scan_all()``, so one
stray byte in a consumer repo aborts that repo's entire multi-series run.  A
crashed check reports nothing, and silence reads as coverage.

This class was fixed three times by enumerating the sites a review listed, and
survived each time through a reader nobody had enumerated.  These tests pin it
at the **public entry point** of every checker this PR touches, so a new reader
added behind any of them is covered without anyone having to list it.

Crucially the corruption is **parametrised over every seeded file**, not fixed on
a sibling ``.py``.  The first version of this file always wrote a valid
``pyproject.toml`` and corrupted only siblings — and both surviving crashes were
reached through ``pyproject.toml``, the file nearly every entry point reads
first.  Rotating the corrupted file is what makes this a class gate rather than
another enumeration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite import runner
from conformance.suite.checks import dependency_conformance, deprecation
from conformance.suite.checks import e2e_generated_harness as harness
from conformance.suite.checks import e2e_workflow_shape as workflow
from conformance.suite.checks import sdr
from conformance.suite.checks import transform_templates as templates
from conformance.suite.checks._ast_common import detect_scope

_BAD = b"\xff\xfe not utf-8 at all\n"


#: Every file the seeded tree contains, so the corruption can be rotated across
#: all of them rather than fixed on one.
_SEEDED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "atlan.yaml",
    "app/connector.py",
    "app/generated/manifest.json",
    "app/generated/_e2e_base.py",
    "app/transformers/column.yaml",
    "tests/e2e/test_full_dag.py",
    ".github/workflows/tests.yaml",
    "contract_schema.lock.json",
)


def _seed(root: Path, corrupt: str) -> None:
    """Write a plausible app tree, with exactly *corrupt* replaced by bad bytes."""
    good: dict[str, str] = {
        "pyproject.toml": (
            '[project]\nname = "demo"\nversion = "0.1.0"\n'
            'dependencies = ["atlan-application-sdk>=3.22,<4.0"]\n'
        ),
        "uv.lock": '[[package]]\nname = "demo"\nversion = "0.1.0"\n',
        "atlan.yaml": "self_deployed_runtime: true\nname: demo\n",
        "app/connector.py": "class C:\n    pass\n",
        "app/generated/manifest.json": "{}\n",
        "app/generated/_e2e_base.py": "class DemoGeneratedE2EBase:\n    pass\n",
        "app/transformers/column.yaml": (
            "columns:\n  attributes:\n    name:\n      source_query: col\n"
        ),
        "tests/e2e/test_full_dag.py": "class TestX:\n    pass\n",
        ".github/workflows/tests.yaml": "name: Tests\non:\n  pull_request:\n",
        "contract_schema.lock.json": '{"version": 1, "fields": []}\n',
    }
    for rel, content in good.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == corrupt:
            path.write_bytes(_BAD)
        else:
            path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("corrupt", _SEEDED_FILES)
@pytest.mark.parametrize(
    "module",
    [sdr, deprecation, harness, workflow],
    ids=["sdr", "deprecation", "e2e_harness", "e2e_workflow"],
)
def test_scan_all_survives_non_utf8_input(tmp_path: Path, module, corrupt: str) -> None:
    """discover() + scan_all() must return, not raise, on undecodable input."""
    _seed(tmp_path, corrupt)
    paths = module.discover(tmp_path)
    findings = module.scan_all(paths, tmp_path)
    assert isinstance(findings, list)


@pytest.mark.parametrize("corrupt", _SEEDED_FILES)
def test_transform_templates_survives_non_utf8_input(
    tmp_path: Path, corrupt: str
) -> None:
    """P040 is wired as discover + scan_path, so both hooks are exercised."""
    _seed(tmp_path, corrupt)
    for path in templates.discover(tmp_path):
        assert isinstance(templates.scan_path(path, tmp_path), list)


@pytest.mark.parametrize("corrupt", _SEEDED_FILES)
def test_dependency_scan_all_survives_non_utf8_input(
    tmp_path: Path, corrupt: str
) -> None:
    """D-series takes a different scan_all signature, so it gets its own case.

    Driven off ``discover()`` rather than hand-picked paths — passing only known
    good files is how the first version of this test missed the unguarded read
    in ``scan_path``.
    """
    _seed(tmp_path, corrupt)
    findings = dependency_conformance.scan_all(
        dependency_conformance.discover(tmp_path),
        tmp_path,
        imported_modules=set(),
        dist_import_map={},
        dialect_drivers=set(),
    )
    assert isinstance(findings, list)


@pytest.mark.parametrize("corrupt", _SEEDED_FILES)
def test_detect_scope_survives_non_utf8_input(tmp_path: Path, corrupt: str) -> None:
    """detect_scope() is the first line of nearly every entry point."""
    _seed(tmp_path, corrupt)
    detect_scope(tmp_path)  # must not raise


@pytest.mark.parametrize("corrupt", _SEEDED_FILES)
def test_runner_main_survives_non_utf8_input(tmp_path: Path, corrupt: str) -> None:
    """The real entry point, covering every registered series at once.

    The per-module cases above are a hand-written allow-list of surfaces; this
    one closes the axis instead of extending it. ``runner.main()`` is what CI
    actually invokes, it wraps neither ``discover()`` nor the scan hooks, and a
    crash on any series means every later series silently never runs.

    This is the test that was missing when a decode-blind reader in a module
    nobody had enumerated took down the whole run while the suite stayed green.
    """
    _seed(tmp_path, corrupt)
    exit_code = runner.main(
        ["--repo", str(tmp_path), "--output", str(tmp_path / "out.sarif")]
    )
    assert isinstance(exit_code, int)

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
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks import dependency_conformance, deprecation
from conformance.suite.checks import e2e_generated_harness as harness
from conformance.suite.checks import e2e_workflow_shape as workflow
from conformance.suite.checks import sdr
from conformance.suite.checks import transform_templates as templates

_BAD = b"\xff\xfe not utf-8 at all\n"


def _seed(root: Path) -> None:
    """A minimally plausible app tree with a non-UTF-8 file in every reader's path."""
    (root / "atlan.yaml").write_bytes(_BAD)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["atlan-application-sdk>=3.22,<4.0"]\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_bytes(_BAD)

    app = root / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "connector.py").write_text("class C:\n    pass\n", encoding="utf-8")
    (app / "bad.py").write_bytes(_BAD)
    gen = app / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "manifest.json").write_bytes(_BAD)
    (gen / "_e2e_base.py").write_bytes(_BAD)

    tests_e2e = root / "tests" / "e2e"
    tests_e2e.mkdir(parents=True, exist_ok=True)
    (tests_e2e / "test_full_dag.py").write_text(
        "class TestX:\n    pass\n", encoding="utf-8"
    )
    (tests_e2e / "test_bad.py").write_bytes(_BAD)

    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "tests.yaml").write_text(
        "name: Tests\non:\n  pull_request:\n", encoding="utf-8"
    )
    (wf / "bad.yaml").write_bytes(_BAD)

    tpl = root / "app" / "transformers"
    tpl.mkdir(parents=True, exist_ok=True)
    (tpl / "column.yaml").write_text(
        "columns:\n  attributes:\n    name:\n      source_query: col\n",
        encoding="utf-8",
    )
    (tpl / "bad.yaml").write_bytes(_BAD)

    (root / "contract_schema.lock.json").write_bytes(_BAD)


@pytest.mark.parametrize(
    "module",
    [sdr, deprecation, harness, workflow],
    ids=["sdr", "deprecation", "e2e_harness", "e2e_workflow"],
)
def test_scan_all_survives_non_utf8_input(tmp_path: Path, module) -> None:
    """discover() + scan_all() must return, not raise, on undecodable input."""
    _seed(tmp_path)
    paths = module.discover(tmp_path)
    findings = module.scan_all(paths, tmp_path)
    assert isinstance(findings, list)


def test_transform_templates_survives_non_utf8_input(tmp_path: Path) -> None:
    """P040 is wired as discover + scan_path, so both hooks are exercised."""
    _seed(tmp_path)
    paths = templates.discover(tmp_path)
    for path in paths:
        assert isinstance(templates.scan_path(path, tmp_path), list)
    # discover() itself reads every candidate YAML, including the bad one.
    assert all(p.suffix in {".yml", ".yaml"} for p in paths)


def test_dependency_scan_all_survives_non_utf8_input(tmp_path: Path) -> None:
    """D-series takes a different scan_all signature, so it gets its own case."""
    _seed(tmp_path)
    findings = dependency_conformance.scan_all(
        [tmp_path / "pyproject.toml", tmp_path / "app" / "connector.py"],
        tmp_path,
        imported_modules=set(),
        dist_import_map={},
        dialect_drivers=set(),
    )
    assert isinstance(findings, list)

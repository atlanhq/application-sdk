"""Tests for the shared scoring primitives.

Scoring itself is covered in ``test_report.py``; derivation in
``test_derive.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conformance.ledger.compute import detect_tenant_access, scan_entrypoints


def _write_app(tmp_path: Path, source: str) -> Path:
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "main.py").write_text(textwrap.dedent(source))
    return tmp_path


# ---------------------------------------------------------------- scanning


def test_scan_finds_entrypoint_decorated_methods(tmp_path):
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, inp): ...

            @entrypoint(name="miner")
            async def miner(self, inp): ...

            @task
            async def fetch_tables(self, inp): ...
        """,
    )
    assert scan_entrypoints(repo) == {"crawler", "miner"}


def test_scan_falls_back_to_run_override_for_single_workflow_apps(tmp_path):
    """connector-bravo, connector-delta and connector-india declare their workflow this way."""
    repo = _write_app(
        tmp_path,
        """
        class MyApp(BaseMetadataExtractor):
            async def run(self, inp): ...
        """,
    )
    assert scan_entrypoints(repo) == {"run"}


def test_scan_ignores_run_when_entrypoints_exist(tmp_path):
    """connector-echo and connector-hotel have both; there run() is the SDK orchestrator."""
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, inp): ...

            async def run(self, inp): ...
        """,
    )
    assert scan_entrypoints(repo) == {"crawler"}


def test_scan_skips_unparseable_modules(tmp_path):
    repo = _write_app(tmp_path, "def broken(:\n")
    assert scan_entrypoints(repo) == set()


# ------------------------------------------------- the integration/e2e line


def _write_test(tmp_path: Path, relpath: str, body: str) -> Path:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body))
    return tmp_path


@pytest.mark.parametrize(
    "marker",
    ["ATLAN_BASE_URL", "ATLAN_API_KEY", "application_sdk.testing.e2e", "pyatlan"],
)
def test_tenant_access_detected_uniformly(tmp_path, marker):
    """One rule covers every repo - not a per-app list of system-app names."""
    repo = _write_test(
        tmp_path,
        "tests/e2e/test_full_dag.py",
        f"""
        import os
        if not os.environ.get("{marker}"):
            pytest.skip("needs a tenant")
        """,
    )
    hit = detect_tenant_access(repo, "tests/e2e/test_full_dag.py")
    assert hit is not None and hit[1] == marker


def test_lane_staying_inside_the_boundary_is_not_flagged(tmp_path):
    repo = _write_test(
        tmp_path,
        "tests/e2e/test_extract.py",
        """
        def test_transformed_output_exists(output_validator):
            output_validator.assert_transformed_output_exists()
        """,
    )
    assert detect_tenant_access(repo, "tests/e2e/test_extract.py") is None


def test_directory_citation_catches_an_e2e_lane_hiding_in_the_tree(tmp_path):
    """A too-broad citation must fail - this caught a real ledger bug."""
    repo = _write_test(
        tmp_path,
        "tests/e2e/test_extract.py",
        "def test_ok(): assert True\n",
    )
    _write_test(
        tmp_path,
        "tests/e2e/sdr/test_full_dag.py",
        """
        import os
        os.environ["ATLAN_BASE_URL"]
        """,
    )
    hit = detect_tenant_access(repo, "tests/e2e/")
    assert hit is not None
    assert "sdr/test_full_dag.py" in hit[0]

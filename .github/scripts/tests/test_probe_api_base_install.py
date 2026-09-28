"""Tests for probe_api_base_install.py (the verdict logic; the probe itself runs in CI)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import probe_api_base_install as probe  # noqa: E402


def _ok(**overrides) -> probe.Probe:
    values = dict(
        failed_imports={},
        loaded_modules=frozenset({"fastapi", "pydantic", "application_sdk_api.server"}),
        route_count=12,
        max_rss_mb=58.0,
    )
    values.update(overrides)
    return probe.Probe(**values)


def test_a_thin_install_passes() -> None:
    assert probe.problems(_ok(), max_rss_mb=120) == []


def test_a_gated_module_may_fail_to_import() -> None:
    p = _ok(failed_imports={"application_sdk_api.workflow.temporal": "ImportError: x"})
    assert probe.problems(p, max_rss_mb=120) == []


def test_any_other_import_failure_fails() -> None:
    p = _ok(
        failed_imports={"application_sdk_api.clients.sql": "ImportError: sqlalchemy"}
    )
    assert any("clients.sql" in line for line in probe.problems(p, max_rss_mb=120))


def test_a_worker_package_in_sys_modules_fails() -> None:
    p = _ok(
        loaded_modules=frozenset(
            {"fastapi", "application_sdk.errors", "temporalio.client"}
        )
    )
    found = probe.problems(p, max_rss_mb=120)
    assert found == [
        "worker-side packages loaded by the base install: ['application_sdk', 'temporalio']"
    ]


def test_a_same_prefix_name_is_not_mistaken_for_the_sdk() -> None:
    p = _ok(loaded_modules=frozenset({"application_sdk_api.errors"}))
    assert probe.problems(p, max_rss_mb=120) == []


def test_an_app_without_routes_fails() -> None:
    assert probe.problems(_ok(route_count=0), max_rss_mb=120)


def test_rss_over_budget_fails() -> None:
    found = probe.problems(_ok(max_rss_mb=150.0), max_rss_mb=120)
    assert found == ["peak RSS 150.0 MB exceeds the 120 MB budget"]

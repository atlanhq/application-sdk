"""Tests for deriving a connector's coverage picture from the repo itself.

The point of this module is that nothing is hand-maintained: the workflow list,
the integration/e2e boundary and what the tests cover are all rebuilt on every
run from generated artifacts and declared fields.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from conformance.ledger.derive import (
    _dag_nodes_to_workflow,
    discover_boundary,
    discover_declared_coverage,
    discover_workflows,
)
from conformance.ledger.schema import Boundary, Depth


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


def _manifest(root: Path, rel: str, dag: dict) -> None:
    _write(root, rel, json.dumps({"dag": dag}))


# ------------------------------------------------------------- workflows


def test_entrypoints_are_the_denominator(tmp_path):
    _write(
        tmp_path,
        "app/main.py",
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, i): ...

            @entrypoint
            async def miner(self, i): ...
        """,
    )
    assert set(discover_workflows(tmp_path)) == {"crawler", "miner"}


def test_run_override_counts_only_without_entrypoints(tmp_path):
    """Single-workflow apps declare via run(); multi-entrypoint apps don't."""
    _write(
        tmp_path,
        "app/main.py",
        """
        class MyApp(BaseMetadataExtractor):
            async def run(self, i): ...
        """,
    )
    assert set(discover_workflows(tmp_path)) == {"run"}

    _write(
        tmp_path,
        "app/main.py",
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, i): ...

            async def run(self, i): ...
        """,
    )
    assert set(discover_workflows(tmp_path)) == {"crawler"}


def test_workflows_carry_their_declaration_site(tmp_path):
    _write(
        tmp_path,
        "app/main.py",
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, i): ...
        """,
    )
    assert discover_workflows(tmp_path)["crawler"].startswith("app/main.py:")


# -------------------------------------------------------------- boundary


@pytest.mark.parametrize(
    ("connector", "workflow_type", "expected"),
    [
        ("atlan-connector-alpha-app", "connector-alpha:crawler", "crawler"),
        ("atlan-connector-golf-app", "connector-golf:process-metadata", "process_metadata"),
        # bare connector name = a single-workflow app, whose workflow is run()
        ("atlan-connector-delta-app", "connector-delta", "run"),
        ("atlan-connector-alpha-app", "PublishWorkflow", None),
        ("atlan-connector-alpha-app", "QueryIntelligenceWorkflow", None),
        ("atlan-connector-alpha-app", "LineageWorkflow", None),
        ("atlan-connector-alpha-app", "", None),
        # a prefix belonging to a different connector is not ours
        ("atlan-connector-alpha-app", "connector-golf:extract-metadata", None),
    ],
)
def test_workflow_type_decides_the_boundary(
    tmp_path, connector, workflow_type, expected
):
    _manifest(
        tmp_path,
        "app/generated/manifest.json",
        {"node": {"inputs": {"workflow_type": workflow_type}}},
    )
    mapping = _dag_nodes_to_workflow(tmp_path, connector)
    assert mapping["generated/node"] == expected


def test_app_name_is_not_used_as_the_discriminator(tmp_path):
    """connector-golf routes its own nodes through automation-engine; connector-juliet ships
    an unresolved {app_name} placeholder. Both must still resolve correctly."""
    _manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "process": {
                "app_name": "automation-engine",
                "inputs": {"workflow_type": "connector-golf:process-metadata"},
            },
            "publish": {
                "app_name": "publish",
                "inputs": {"workflow_type": "PublishWorkflow"},
            },
        },
    )
    boundary = discover_boundary(tmp_path, "atlan-connector-golf-app")
    assert boundary["generated/process"] is Boundary.TRANSFORMED
    assert boundary["generated/publish"] is Boundary.POST_PUBLISH


def test_per_manifest_nodes_do_not_collide(tmp_path):
    """A multi-entrypoint app ships one manifest per entrypoint, each with its
    own `extract` node. Keying on the bare node name would lose all but one."""
    _manifest(
        tmp_path,
        "app/generated/crawler/manifest.json",
        {"extract": {"inputs": {"workflow_type": "connector-alpha:crawler"}}},
    )
    _manifest(
        tmp_path,
        "app/generated/miner/manifest.json",
        {"extract": {"inputs": {"workflow_type": "connector-alpha:miner"}}},
    )
    mapping = _dag_nodes_to_workflow(tmp_path, "atlan-connector-alpha-app")
    assert mapping["crawler/extract"] == "crawler"
    assert mapping["miner/extract"] == "miner"


def test_connector_stem_variants_are_accepted(tmp_path):
    """Manifests name the app variously: connector-juliet ships `connector-juliet-app:`."""
    _manifest(
        tmp_path,
        "app/generated/manifest.json",
        {"extract": {"inputs": {"workflow_type": "connector-juliet-app:extract-metadata"}}},
    )
    mapping = _dag_nodes_to_workflow(tmp_path, "atlan-connector-juliet-app")
    assert mapping["generated/extract"] == "extract_metadata"


# ------------------------------------------------------ declared coverage


def test_declared_entrypoint_is_read_from_the_scenario(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        """
        scenarios = [
            Scenario(name="crawl", api="workflow", assert_that={},
                     entrypoint="crawler", schema_base_path="tests/schema"),
        ]
        """,
    )
    coverage = discover_declared_coverage(tmp_path)
    assert coverage.depth == {"crawler": Depth.VALIDATED}


def test_expected_data_outranks_schema_validation(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        """
        Scenario(name="a", api="workflow", assert_that={}, entrypoint="crawler",
                 schema_base_path="s")
        Scenario(name="b", api="workflow", assert_that={}, entrypoint="crawler",
                 expected_data="golden/crawler.json")
        """,
    )
    assert discover_declared_coverage(tmp_path).depth["crawler"] is Depth.GOLDEN


def test_scenario_without_validation_declares_only_counts_depth(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        'Scenario(name="a", api="workflow", assert_that={}, entrypoint="miner")\n',
    )
    assert discover_declared_coverage(tmp_path).depth["miner"] is Depth.COUNTS


def test_class_level_entrypoint_is_honoured(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        """
        class TestSuite(BaseIntegrationTest):
            entrypoint = "crawler"
            scenarios = [
                Scenario(name="a", api="workflow", assert_that={},
                         schema_base_path="s"),
            ]
        """,
    )
    assert discover_declared_coverage(tmp_path).depth == {"crawler": Depth.VALIDATED}


def test_skipped_scenarios_do_not_count(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        """
        Scenario(name="a", api="workflow", assert_that={}, entrypoint="miner",
                 skip=True, skip_reason="baseline not captured")
        """,
    )
    assert discover_declared_coverage(tmp_path).depth == {}


def test_undeclared_scenarios_are_not_attributed(tmp_path):
    """Without a declared entrypoint there is nothing to attribute - which is
    what T020 exists to surface."""
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        'Scenario(name="a", api="workflow", assert_that={})\n',
    )
    assert discover_declared_coverage(tmp_path).depth == {}


def test_non_workflow_scenarios_are_ignored(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        'Scenario(name="a", api="auth", assert_that={}, entrypoint="crawler")\n',
    )
    assert discover_declared_coverage(tmp_path).depth == {}


def test_coverage_records_its_sources(tmp_path):
    _write(
        tmp_path,
        "tests/integration/test_x.py",
        'Scenario(name="a", api="workflow", assert_that={}, entrypoint="crawler")\n',
    )
    sources = discover_declared_coverage(tmp_path).sources["crawler"]
    assert sources and sources[0].startswith("tests/integration/test_x.py:")


def test_missing_directories_are_not_an_error(tmp_path):
    assert discover_workflows(tmp_path) == {}
    assert discover_boundary(tmp_path, "atlan-x-app") == {}
    assert discover_declared_coverage(tmp_path).depth == {}
